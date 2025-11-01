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
import base64
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
import json

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
    'sqlite:///:memory:', 
    connect_args={"check_same_thread": False},
    poolclass=StaticPool  # Use static pool for SQLite to avoid threading issues
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def generate_working_vapid_keys():
    """
    Generate VAPID keys in the exact format that pywebpush expects
    """
    # Generate EC private key
    private_key = ec.generate_private_key(ec.SECP256R1())
    
    # Get the raw private key bytes (32 bytes for P-256)
    private_key_raw = private_key.private_numbers().private_value.to_bytes(32, 'big')
    
    # Get the public key in uncompressed format (65 bytes)
    public_key = private_key.public_key()
    public_key_raw = public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint
    )
    
    # Convert to URL-safe base64 without padding
    VAPID_PUBLIC_KEY = base64.urlsafe_b64encode(public_key_raw).decode('utf-8').replace('=', '')
    VAPID_PRIVATE_KEY = base64.urlsafe_b64encode(private_key_raw).decode('utf-8').replace('=', '')
    
    print("✅ Generated working VAPID keys:")
    print(f"Public Key: {VAPID_PUBLIC_KEY}")
    print(f"Public Key Length: {len(VAPID_PUBLIC_KEY)}")
    print(f"Private Key Length: {len(VAPID_PRIVATE_KEY)}")
    
    return VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY
VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY = generate_working_vapid_keys()
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


# Improved background check task
def check_inactive_users():
    print(f"Checking inactive users at {datetime.utcnow()}")
    
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
                    # Update last_active to prevent spamming
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
        db.close()

# Configure scheduler properly
executors = {
    'default': ThreadPoolExecutor(2)  # Increased to 2 threads
}

job_defaults = {
    'coalesce': False,  # Don't combine skipped executions
    'max_instances': 2,  # Allow 2 concurrent instances
    'misfire_grace_time': 120  # 2 minute grace period
}

scheduler = BackgroundScheduler(
    executors=executors,
    job_defaults=job_defaults
)

scheduler = BackgroundScheduler(
    executors=executors,
    job_defaults=job_defaults
)

scheduler.add_job(
    check_inactive_users, 
    "interval", 
    minutes=2,
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

@app.get("/test-subscription/{user_name}")
def test_subscription(user_name: str):
    """Endpoint to test a specific user's subscription"""
    db = SessionLocal()
    user = db.query(User).filter(User.name == user_name).first()
    db.close() 
    
    if not user:
        return {"error": "User not found"}
    
    if not user.subscription:
        return {"error": "User has no subscription"}
    
    # Test the subscription
    success = send_push_notification(
        user.subscription,
        title="Test Notification",
        message="This is a test notification from the server!"
    )
    
    return {
        "user": user.name,
        "has_subscription": user.subscription is not None,
        "test_success": success,
        "endpoint": user.subscription.get('endpoint', 'unknown') if user.subscription else 'none'
    }


# Shutdown scheduler when app stops
@app.on_event("shutdown")
def shutdown_event():
    if scheduler.running:
        scheduler.shutdown()
        print("Scheduler shut down")
    # Close all database connections
    engine.dispose()
