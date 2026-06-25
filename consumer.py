import asyncio
import json
import logging
import uuid
import os
from dotenv import load_dotenv
import aio_pika
from sqlalchemy.future import select

from database import AsyncSessionLocal
from models import Notification, ProcessedEvent
from email_service import send_email_async

logger = logging.getLogger("NotificationConsumer")

load_dotenv()

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@127.0.0.1:5672/")

STATUS_MESSAGES = {
    "CREATED": "Buyurtmangiz muvaffaqiyatli qabul qilindi!",
    "CONFIRMED": "Buyurtmangiz tasdiqlandi. Restoran taomni tayyorlashni boshlamoqda.",
    "PREPARING": "Taomingiz tayyorlanmoqda.",
    "READY": "Buyurtmangiz tayyor! Kuryer uni tez orada olib ketadi.",
    "DELIVERING": "Buyurtmangiz yo'lda! Kuryer manzilingizga yaqinlashmoqda.",
    "DELIVERED": "Buyurtmangiz yetkazib berildi. Yoqimli ishtaha!",
    "CANCELLED": "Buyurtmangiz bekor qilindi.",
}

# asyncio keeps only a WEAK reference to tasks, so a fire-and-forget task can be
# garbage-collected mid-run. Hold a strong reference until it finishes.
_background_tasks: set = set()


def _fire_background(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def handle_email_background(to_email, subject, status_msg, order_id, items, total_price, currency):
    """Best-effort email. A failure here must NOT affect the ack: the in-DB
    notification is the source of truth, the email is a bonus on top."""
    logger.info(f"⏳ [BACKGROUND EMAIL] Dispatching to {to_email}...")
    try:
        await send_email_async(
            to_email=to_email, subject=subject, status_msg=status_msg,
            order_id=order_id, items=items, total_price=total_price, currency=currency,
        )
        logger.info(f"✅ [BACKGROUND EMAIL] Sent to {to_email}")
    except Exception as em_err:
        logger.error(f"❌ [BACKGROUND EMAIL] Failed for {to_email} (DB already safe): {em_err}")


async def _persist_event(event_id, event_type, data):
    """Does the durable work in ONE transaction: idempotency marker + notifications.

    Returns an email_metadata tuple on success, or None when there's nothing to
    email (duplicate / unknown type). Raises on failure so the caller can decide
    whether to retry.

    NOTE on ID types (post-migration):
      - order_id   -> UUID  (unchanged, Order Service still issues UUID order IDs)
      - event_id   -> UUID  (unchanged, internal idempotency key)
      - customerId / restaurantId / courierId -> int (CHANGED to match the
        Auth Service's integer user IDs and the new Notification.user_id column)
    """
    async with AsyncSessionLocal() as db:
        # Idempotency: did we already COMMIT this event before?
        existing = await db.execute(select(ProcessedEvent).filter_by(event_id=event_id))
        if existing.scalar_one_or_none():
            logger.warning(f"⚠️ [DUPLICATE] Already processed, skipping: {event_id}")
            return None

        order_id = uuid.UUID(data["orderId"])       # still UUID
        customer_id = int(data["customerId"])        # now int
        customer_email = data.get("customerEmail")
        items = data.get("items", [])
        total_price = data.get("totalPrice", 0)
        currency = data.get("currency", "UZS")

        notifications = []
        email_metadata = None

        if event_type == "order.created":
            title = "Yangi Buyurtma"
            body_text = STATUS_MESSAGES["CREATED"]
            notifications.append(Notification(
                user_id=customer_id, order_id=order_id,
                type="ORDER_CREATED", title=title, body=body_text,
            ))
            if customer_email:
                email_metadata = (customer_email, title, body_text, str(order_id), items, total_price, currency)
            if data.get("restaurantId"):
                notifications.append(Notification(
                    user_id=int(data["restaurantId"]), order_id=order_id,
                    type="ORDER_CREATED", title="Yangi Buyurtma Keldi!",
                    body=f"Restoraningizga yangi buyurtma tushdi (ID: {str(order_id)[:8]}).",
                ))

        elif event_type == "order.status_changed":
            new_status = data["newStatus"]
            title = f"Buyurtma holati: {new_status}"
            body_text = STATUS_MESSAGES.get(new_status, "Buyurtma holati yangilandi.")
            notifications.append(Notification(
                user_id=customer_id, order_id=order_id,
                type="STATUS_CHANGED", title=title, body=body_text,
            ))
            if customer_email:
                email_metadata = (customer_email, title, body_text, str(order_id), items, total_price, currency)
            if data.get("courierId"):
                notifications.append(Notification(
                    user_id=int(data["courierId"]), order_id=order_id,
                    type="STATUS_CHANGED", title="Sizga buyurtma biriktirildi",
                    body=f"Buyurtmani mijozga yetkazishni boshlang (ID: {str(order_id)[:8]}).",
                ))
        else:
            logger.warning(f"❓ [UNKNOWN TYPE] Discarding: {event_type}")
            return None

        # The notifications AND the 'processed' marker commit together, atomically.
        # Either both land or neither does -- THIS is what makes a retry safe:
        # a re-delivered message will see the marker and skip.
        for n in notifications:
            db.add(n)
        db.add(ProcessedEvent(event_id=event_id))
        await db.commit()
        logger.info(f"💾 [COMMITTED] {len(notifications)} notification(s) for event {event_id}")

        return email_metadata


async def process_event(message: aio_pika.IncomingMessage):
    # --- Phase 1: parse the envelope. A message that isn't valid JSON / is missing
    # core fields will NEVER succeed on retry, so we ack (drop) it rather than
    # requeue it into an infinite poison loop. ---
    try:
        body = json.loads(message.body.decode("utf-8"))
        event_id = uuid.UUID(body["eventId"])
        event_type = body["eventType"]
        data = body["data"]
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        logger.error(f"🧨 [POISON] Unparseable envelope, dropping: {e}")
        await message.ack()
        return

    # --- Phase 2: do the durable work. ---
    try:
        email_metadata = await _persist_event(event_id, event_type, data)
    except (KeyError, ValueError, TypeError) as e:
        # Bad payload SHAPE (e.g. missing data.orderId, or customerId isn't a
        # valid int after the integer-ID migration). Retrying won't fix it -> drop.
        # IMPORTANT: if real events still send UUID strings for customerId /
        # restaurantId / courierId, int(...) will raise ValueError HERE and the
        # event gets silently dropped with no notification ever created.
        # Confirm the Order Service's actual payload shape before relying on this.
        logger.error(f"🧨 [POISON] Bad event shape for {event_id}, dropping: {e}")
        await message.ack()
        return
    except Exception as e:
        # Infrastructure failure (DB down, etc.). Assume transient -> requeue & retry.
        # Idempotency makes the retry safe; a committed duplicate just gets skipped.
        logger.error(f"🔁 [REQUEUE] Transient failure on {event_id}, will retry: {e}", exc_info=True)
        await message.reject(requeue=True)
        return

    # --- Success: the work is durably committed, so it's now safe to ack.
    # ONLY after acking do we fire the best-effort email. ---
    await message.ack()
    if email_metadata:
        _fire_background(handle_email_background(*email_metadata))


async def start_consumer():
    while True:
        try:
            logger.info(f"🔌 [AMQP CONNECT] {RABBITMQ_URL}")
            connection = await aio_pika.connect_robust(RABBITMQ_URL, timeout=5)
            async with connection:
                channel = await connection.channel()
                # Cap how many unacked messages the broker pushes at once -- keeps
                # memory bounded and stops one consumer hoarding the whole queue.
                await channel.set_qos(prefetch_count=10)

                exchange = await channel.declare_exchange(
                    "foodexpress", type=aio_pika.ExchangeType.TOPIC, durable=True,
                )
                queue = await channel.declare_queue("notification-service-queue", durable=True)
                await queue.bind(exchange, routing_key="order.created")
                await queue.bind(exchange, routing_key="order.status_changed")

                logger.info("🚀 [READY] Watching [order.created, order.status_changed]...")
                await queue.consume(process_event)
                await asyncio.Future()  # run forever until cancelled

        except asyncio.CancelledError:
            logger.info("🛑 [SHUTDOWN] Consumer cancelled, exiting cleanly.")
            break
        except (aio_pika.exceptions.AMQPConnectionError, asyncio.TimeoutError) as err:
            logger.warning(f"🔄 [CONN FAILED] {err}. Retrying in 5s...")
            await asyncio.sleep(5)
        except Exception as err:
            logger.error(f"💥 [UNKNOWN] {err}. Retrying in 5s...", exc_info=True)
            await asyncio.sleep(5)