from fastapi import FastAPI, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from pywebpush import webpush, WebPushException
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from datetime import datetime, timedelta
from sqlalchemy import create_engine, Column, Integer, String, DateTime, JSON
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from sqlalchemy.pool import StaticPool
from urllib.parse import urlparse
import json
import base64
import os

app = FastAPI()

# Allow frontend access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Database setup with thread-local sessions
Base = declarative_base()

# Use check_same_thread=False for SQLite to allow connection sharing
engine = create_engine(
    "sqlite:///:memory:", 
    connect_args={"check_same_thread": False},
    poolclass=StaticPool  # Use static pool for SQLite to avoid threading issues
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# VAPID Keys
VAPID_PUBLIC_KEY = "BLMOSLUdMfRfx-5cD967p7Y_iEcFkbNLRt_o6ZKpFynNjhla6uWVczoDm5BCzj41d3xwUCdUqmRvpl6mJASIdvw"
VAPID_PRIVATE_KEY = "your_actual_private_key_here"
VAPID_CLAIMS = {"sub": "mailto:you@example.com"}

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    subscription = Column(JSON)
    last_active = Column(DateTime)

Base.metadata.create_all(bind=engine)

# Database dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.get("/vapid-public-key")
def get_vapid_public_key():
    return {"publicKey": VAPID_PUBLIC_KEY}

@app.post("/subscribe")
async def subscribe(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    
    user = db.query(User).filter(User.name == data["name"]).first()
    if not user:
        user = User(name=data["name"], subscription=data["subscription"], last_active=datetime.utcnow())
        db.add(user)
    else:
        user.subscription = data["subscription"]
        user.last_active = datetime.utcnow()
    
    db.commit()
    return {"status": "subscribed"}

@app.post("/heartbeat")
async def heartbeat(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    
    user = db.query(User).filter(User.name == data["name"]).first()
    if user:
        user.last_active = datetime.utcnow()
        db.commit()
    
    return {"status": "heartbeat updated"}

def get_vapid_claims(subscription_info):
    """Generate correct VAPID claims based on the subscription endpoint"""
    if not subscription_info or 'endpoint' not in subscription_info:
        return {"sub": "mailto:you@example.com"}
    
    endpoint = subscription_info['endpoint']
    
    if 'fcm.googleapis.com' in endpoint:
        aud = 'https://fcm.googleapis.com'
    else:
        parsed = urlparse(endpoint)
        aud = f"{parsed.scheme}://{parsed.netloc}"
    
    return {
        "sub": "mailto:you@example.com",
        "aud": aud
    }

def send_push_notification(subscription, title, message):
    if not subscription:
        print("No subscription provided")
        return False
        
    payload = json.dumps({
        "title": title,
        "body": message,
        "icon": "https://via.placeholder.com/64"
    })
    
    try:
        print(f"Sending notification to user...")
        
        vapid_claims = get_vapid_claims(subscription)
        print(f"Using VAPID audience: {vapid_claims.get('aud', 'unknown')}")
        
        webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims=vapid_claims,
            timeout=10
        )
        print("Notification sent successfully")
        return True
        
    except WebPushException as e:
        print(f"WebPush failed: {e}")
        if hasattr(e, 'response') and e.response:
            print(f"Response status: {e.response.status_code}")
            if e.response.status_code == 410:
                remove_expired_subscription(subscription)
        return False
        
    except Exception as e:
        print(f"Unexpected error: {type(e).__name__}: {e}")
        return False

def remove_expired_subscription(subscription):
    """Remove expired subscription from database using a new session"""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.subscription == subscription).first()
        if user:
            user.subscription = None
            db.commit()
            print(f"Removed expired subscription for {user.name}")
    except Exception as e:
        print(f"Error removing subscription: {e}")
    finally:
        db.close()

def check_inactive_users():
    """Background task to check inactive users - creates its own database session"""
    print(f"Checking inactive users at {datetime.utcnow()}")
    
    # Create a new database session for this background task
    db = SessionLocal()
    try:
        inactive_time = datetime.utcnow() - timedelta(minutes=1)
        inactive_users = db.query(User).filter(User.last_active < inactive_time).all()

        print(f"Found {len(inactive_users)} inactive users")
        
        successful_notifications = 0
        failed_notifications = 0
        
        for user in inactive_users:
            if user.subscription:
                print(f"Notifying inactive user: {user.name}")
                success = send_push_notification(
                    user.subscription,
                    title="Hey there!",
                    message="You haven't visited the app for a while. Come back!"
                )
                
                if success:
                    successful_notifications += 1
                    user.last_active = datetime.utcnow()
                else:
                    failed_notifications += 1
                    print(f"Failed to send notification to {user.name}")
                    
                db.commit()
            else:
                print(f"User {user.name} has no subscription")
                
        print(f"Notification summary: {successful_notifications} successful, {failed_notifications} failed")
                
    except Exception as e:
        print(f"Error in check_inactive_users: {e}")
    finally:
        db.close()  # Always close the session

# Scheduler configuration
executors = {
    'default': ThreadPoolExecutor(1)
}

job_defaults = {
    'coalesce': True,
    'max_instances': 1,
    'misfire_grace_time': 60
}

scheduler = BackgroundScheduler(
    executors=executors,
    job_defaults=job_defaults
)

scheduler.add_job(
    check_inactive_users, 
    "interval", 
    minutes=1,
    id="inactive_users_check"
)

try:
    scheduler.start()
    print("Scheduler started successfully")
except Exception as e:
    print(f"Scheduler start failed: {e}")

@app.get("/")
def root():
    return {"message": "FastAPI Push Notification Service Running"}

@app.post("/send-test-notification")
async def send_test_notification(request: Request, db: Session = Depends(get_db)):
    """Send a test notification to a specific user"""
    data = await request.json()
    user_name = data.get("name")
    
    user = db.query(User).filter(User.name == user_name).first()
    
    if not user:
        return {"error": "User not found"}
    
    if not user.subscription:
        return {"error": "User has no subscription"}
    
    print(f"Sending TEST notification to {user_name}")
    
    success = send_push_notification(
        user.subscription,
        title="Test Notification",
        message=f"This is a test notification sent at {datetime.utcnow().strftime('%H:%M:%S')}"
    )
    
    return {
        "user": user_name,
        "has_subscription": user.subscription is not None,
        "test_success": success
    }

# Shutdown scheduler when app stops
@app.on_event("shutdown")
def shutdown_event():
    if scheduler.running:
        scheduler.shutdown()
        print("Scheduler shut down")
    # Close all database connections
    engine.dispose()
