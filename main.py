import asyncio
import sys
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import logging
import os
from contextlib import asynccontextmanager
from typing import List
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from consumer import start_consumer
from database import Base, engine, get_db
from dotenv import load_dotenv
from models import Notification
from schemas import NotificationResponse

# Yangi xavfsizlik tizimi va modelni import qilamiz
from security import CurrentUser, PermissionChecker
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

logging.basicConfig(level=logging.INFO)

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("📢 [LIFESPAN] Initializing database tables...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    print("📢 [LIFESPAN] Registering RabbitMQ Consumer task...")
    # Using background task registration so FastAPI can finish starting up seamlessly
    consumer_task = asyncio.create_task(start_consumer())
    
    yield
    
    print("📢 [LIFESPAN] Shutting down background tasks...")
    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError:
        print("📢 [LIFESPAN] Consumer task cancelled successfully.")

# Pass lifespan right into the application constructor
app = FastAPI(title="FoodExpress - Notification Service", version="1.0.0", lifespan=lifespan)

origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
    "http://localhost:3000",
    "http://127.0.0.1:3000",        
]


app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    return {"status": "healthy"}

@app.get("/notifications", response_model=List[NotificationResponse])
async def get_my_notifications(
    db: AsyncSession = Depends(get_db), 
    # SIZNING MATRITSA: CUSTOMER, RESTAURANT, COURIER hammasida 'order.read.own' bor.
    # Bu dependency tokenni tekshiradi va unda shu permission borligini tasdiqlaydi.
    current_user: CurrentUser = Depends(PermissionChecker("order.read.own"))
):
    # current_user.id ichida avtomat ravishda token ichidagi 'sub' (userId) bo'ladi
    user_id = current_user.id
    
    result = await db.execute(
        select(Notification)
        .filter_by(user_id=user_id)
        .order_by(Notification.created_at.desc())
    )
    return result.scalars().all()

@app.patch("/notifications/{id}/read", response_model=NotificationResponse)
async def mark_notification_as_read(
    id: UUID, 
    db: AsyncSession = Depends(get_db), 
    # Bu erda ham eshikni 'order.read.own' orqali ochamiz
    current_user: CurrentUser = Depends(PermissionChecker("order.read.own"))
):
    user_id = current_user.id
    
    # Faqat so'rov yuborgan user_id va bildirishnoma ID si mos kelsagina ma'lumotni topadi
    result = await db.execute(select(Notification).filter_by(id=id, user_id=user_id))
    notification = result.scalar_one_or_none()
    
    if not notification:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="Notification not found or access denied"
        )
        
    notification.is_read = True
    await db.commit()
    await db.refresh(notification)
    return notification


if __name__ == "__main__":
    import uvicorn
    app_port = int(os.getenv("PORT", 8002))
    print(f"--- Loading configurations, starting app on port {app_port} ---")
    uvicorn.run("main:app", host="0.0.0.0", port=app_port, reload=True)