# FoodExpress — Notification Service

> **Texnologiya:** Python / FastAPI · PostgreSQL (`notification_db`) · RabbitMQ · Port `8002`
> **Markaziy hujjatlar:** [foodexpress-docs](https://github.com/javlonbek16/foodexpress-docs)

## 1. Servis maqsadi

Order servis chiqargan **RabbitMQ eventlarini tinglaydi** va foydalanuvchiga xabar yuboradi. O'quv bosqichida "xabar" = log + DB'ga yozish (real SMS/email mock).

## 2. Talablar (intern nima qilishi kerak)

### Funksional
- [ ] RabbitMQ consumer: `order.created` va `order.status_changed` eventlarini tinglash.
- [ ] Har event uchun "notification" yozuvini DB'ga saqlash va log chiqarish.
- [ ] `order.created` → mijozga "buyurtma qabul qilindi" xabari.
- [ ] `order.status_changed` → har holat o'zgarishi haqida xabar (matn holatga qarab).
- [ ] Foydalanuvchi o'z xabarlarini ko'rishi (`GET /notifications`, `order.read.own`).

### Texnik
- [ ] Event formati **aniq** [EVENTS.md](https://github.com/javlonbek16/foodexpress-docs/blob/main/EVENTS.md) dagidek o'qiladi (konvert: `eventId`, `eventType`, `data`...).
- [ ] **Idempotentlik:** `eventId` bo'yicha takror event ikkinchi marta qayta ishlanmaydi.
- [ ] Xabarlarni o'qish endpointida JWT lokal tekshiruv.
- [ ] Swagger: `http://localhost:8002/docs`. `GET /health` sog'liq endpointi.

## 3. Ma'lumotlar modeli (`notification_db`)

**notifications** — `id` (UUID), `user_id`, `order_id`, `type` (ORDER_CREATED / STATUS_CHANGED), `title`, `body`, `is_read`, `created_at`
**processed_events** — `event_id` (PK, UUID), `processed_at` — idempotentlik uchun

## 4. Asosiy endpointlar

| Method | Path | Permission |
|---|---|---|
| GET | `/notifications` | `order.read.own` |
| PATCH | `/notifications/{id}/read` | `order.read.own` |
| GET | `/health` | — |

## 5. Acceptance criteria
- ✅ Order servis event chiqarganda, Notification uni qabul qiladi va `notifications` ga yozadi.
- ✅ Bir xil `eventId` ikki marta kelsa, faqat bir marta qayta ishlanadi.
- ✅ Foydalanuvchi faqat o'z xabarlarini ko'radi.
- ✅ RabbitMQ ulanishi uzilib qayta ulanganda consumer tiklanadi.

## 6. Arxitektura chegaralari
- ❌ API Gateway yo'q. ❌ Boshqa baza yo'q. ✅ Faqat lokal. Real SMS/email — mock.

## 7. O'rganish maqsadi
Event-driven arxitektura (consumer), RabbitMQ subscribe, idempotentlik, eventual consistency tushunchasi.
