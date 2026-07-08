"""Core utilities: MongoDB Atlas-backed data layer, auth helpers, seed data."""
from __future__ import annotations
import json
import logging
import os
import re
import uuid
import jwt
import bcrypt
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from dotenv import load_dotenv
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection
from pymongo.errors import PyMongoError

load_dotenv()

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret")
JWT_ALG = os.environ.get("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = int(os.environ.get("JWT_EXPIRE_MINUTES", "1440"))
EMERGENT_LLM_KEY = os.environ.get("EMERGENT_LLM_KEY", "")
AWS_S3_BUCKET = os.environ.get("AWS_S3_BUCKET", "")
AWS_S3_REGION = os.environ.get("AWS_S3_REGION", "")
AWS_S3_ENDPOINT_URL = os.environ.get("AWS_S3_ENDPOINT_URL", "")
AWS_S3_UPLOAD_PREFIX = os.environ.get("AWS_S3_UPLOAD_PREFIX", "question-images/")
AWS_S3_PUBLIC_READ = os.environ.get("AWS_S3_PUBLIC_READ", "false").lower() in ("1", "true", "yes")
MONGODB_URI = (
    os.environ.get("MONGODB_URI")
    or os.environ.get("MONGO_URL")
    or "mongodb+srv://megatronbadr96_db_user:4NH4kSsYtzLvKQrx@cluster0.9iwtjzt.mongodb.net/"
)
MONGODB_DB_NAME = (
    os.environ.get("MONGODB_DB_NAME")
    or os.environ.get("DB_NAME")
    or "gyansai"
)
MONGODB_COLLECTION_NAME = (
    os.environ.get("MONGODB_COLLECTION_NAME")
    or os.environ.get("DB_COLLECTION")
    or "gyansai"
)

logger = logging.getLogger(__name__)


class UpdateResult:
    def __init__(self, matched_count: int = 0, modified_count: int = 0, upserted_id: Optional[str] = None):
        self.matched_count = matched_count
        self.modified_count = modified_count
        self.upserted_id = upserted_id


class MongoCursor:
    def __init__(self, cursor, clean_fn):
        self._cursor = cursor
        self._clean_fn = clean_fn

    def sort(self, field: str, direction: int = 1):
        self._cursor = self._cursor.sort(field, direction)
        return self

    def limit(self, value: int):
        self._cursor = self._cursor.limit(value)
        return self

    async def to_list(self, limit: Optional[int] = None):
        if limit is not None:
            docs = await self._cursor.to_list(limit)
        else:
            docs = []
            async for doc in self._cursor:
                docs.append(doc)
        return [self._clean_fn(doc) for doc in docs]


class MongoCollection:
    def __init__(self, root_collection: AsyncIOMotorCollection, name: str):
        self._root_collection = root_collection
        self.name = name

    def _wrap_filter(self, query: Optional[dict] = None) -> dict:
        base_filter = {"collection_name": self.name}
        if not query:
            return base_filter
        if not isinstance(query, dict):
            return base_filter
        if any(key in query for key in ("$and", "$or", "$nor")):
            return {"$and": [base_filter, query]}
        return {**base_filter, **query}

    def _clean_doc(self, doc: dict) -> dict:
        if not doc:
            return doc
        doc.pop("_id", None)
        doc.pop("collection_name", None)
        return doc

    def _wrap_projection(self, projection: Optional[dict] = None) -> Optional[dict]:
        if projection is None:
            return None
        return dict(projection)

    async def find_one(self, query: Optional[dict] = None, projection: Optional[dict] = None):
        doc = await self._root_collection.find_one(self._wrap_filter(query), self._wrap_projection(projection))
        return self._clean_doc(doc) if doc else None

    def find(self, query: Optional[dict] = None, projection: Optional[dict] = None) -> MongoCursor:
        cursor = self._root_collection.find(self._wrap_filter(query), self._wrap_projection(projection))
        return MongoCursor(cursor, self._clean_doc)

    async def insert_one(self, doc: dict):
        doc = dict(doc)
        if "id" not in doc:
            doc["id"] = str(uuid.uuid4())
        doc["collection_name"] = self.name
        await self._root_collection.insert_one(doc)
        return self._clean_doc(doc)

    async def insert_many(self, docs: list[dict]):
        prepared = []
        for doc in docs:
            item = dict(doc)
            if "id" not in item:
                item["id"] = str(uuid.uuid4())
            item["collection_name"] = self.name
            prepared.append(item)
        if prepared:
            await self._root_collection.insert_many(prepared)
        return None

    async def update_one(self, query: dict, update: dict, upsert: bool = False):
        wrapped_query = self._wrap_filter(query)
        update = dict(update)
        if upsert:
            set_on_insert = update.get("$setOnInsert", {})
            if not isinstance(set_on_insert, dict):
                set_on_insert = {}
            set_on_insert.setdefault("id", str(uuid.uuid4()))
            set_on_insert["collection_name"] = self.name
            update["$setOnInsert"] = set_on_insert
            result = await self._root_collection.update_one(wrapped_query, update, upsert=True)
        else:
            result = await self._root_collection.update_one(wrapped_query, update)
        upserted_id = str(result.upserted_id) if getattr(result, "upserted_id", None) else None
        return UpdateResult(result.matched_count, result.modified_count, upserted_id)

    async def update_many(self, query: dict, update: dict):
        result = await self._root_collection.update_many(self._wrap_filter(query), update)
        return UpdateResult(result.matched_count, result.modified_count, None)

    async def delete_one(self, query: dict):
        result = await self._root_collection.delete_one(self._wrap_filter(query))
        return result

    async def delete_many(self, query: dict):
        result = await self._root_collection.delete_many(self._wrap_filter(query))
        return result

    async def count_documents(self, query: Optional[dict] = None):
        return await self._root_collection.count_documents(self._wrap_filter(query))

    async def distinct(self, field: str, query: Optional[dict] = None):
        values = await self._root_collection.distinct(field, self._wrap_filter(query))
        return [value for value in values if value is not None]

    async def aggregate(self, pipeline: list[dict]):
        from bson import ObjectId

        wrapped_pipeline = [{"$match": {"collection_name": self.name}}] + pipeline
        cursor = self._root_collection.aggregate(wrapped_pipeline)
        docs = []
        async for doc in cursor:
            if not doc:
                docs.append(doc)
                continue
            # Preserve aggregation _id values (they may be the grouped key).
            # Only strip MongoDB object `_id` values (ObjectId instances) which are internal.
            cleaned = dict(doc)
            if isinstance(cleaned.get("_id"), ObjectId):
                cleaned.pop("_id", None)
            cleaned.pop("collection_name", None)
            docs.append(cleaned)
        return docs


class MongoDatabase:
    def __init__(self, database, root_collection_name: str):
        self._root_collection = database[root_collection_name]
        self._collections: dict[str, MongoCollection] = {}

    def get_collection(self, name: str) -> MongoCollection:
        if name not in self._collections:
            self._collections[name] = MongoCollection(self._root_collection, name)
        return self._collections[name]

    def __getattr__(self, name: str) -> MongoCollection:
        return self.get_collection(name)

    def __getitem__(self, name: str) -> MongoCollection:
        return self.get_collection(name)

    async def ensure_indexes(self):
        await self._root_collection.create_index(
            [("collection_name", 1), ("id", 1)], unique=True
        )


client: Optional[AsyncIOMotorClient] = None

class MongoDatabaseProxy:
    def __init__(self, db_name: str, root_collection_name: str, uri: str):
        self._db_name = db_name
        self._root_collection_name = root_collection_name
        self._uri = uri
        self._delegate: Optional[MongoDatabase] = None

    def _init(self):
        if self._delegate is None:
            client = get_mongo_client()
            self._delegate = MongoDatabase(client[self._db_name], self._root_collection_name)

    def get_collection(self, name: str) -> MongoCollection:
        self._init()
        return self._delegate.get_collection(name)

    def __getattr__(self, name: str):
        self._init()
        return getattr(self._delegate, name)

    def __getitem__(self, name: str) -> MongoCollection:
        self._init()
        return self._delegate[name]


async def _handle_db_error(self, coro):
    try:
        return await coro
    except PyMongoError as exc:
        logger.exception("MongoDB operation failed: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable")


def get_mongo_client() -> AsyncIOMotorClient:
    global client
    if client is None:
        logger.info("Initializing MongoDB client")
        client = AsyncIOMotorClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=5000,
            maxPoolSize=5,
        )
    return client


def close_mongo_client() -> None:
    global client
    if client is not None:
        try:
            client.close()
        except Exception as exc:
            logger.warning("Error closing MongoDB client: %s", exc)
        finally:
            client = None


def safe_db_error(fn):
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except PyMongoError as exc:
            logger.exception("MongoDB operation failed: %s", exc)
            raise HTTPException(status_code=503, detail="Database unavailable")
    return wrapper


db = MongoDatabaseProxy(MONGODB_DB_NAME, MONGODB_COLLECTION_NAME, MONGODB_URI)

bearer_scheme = HTTPBearer(auto_error=False)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def create_token(user_id: str, role: str) -> str:
    payload = {
        "sub": user_id,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")


async def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> dict:
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    payload = decode_token(creds.credentials)
    user_id = payload.get("sub")
    role = payload.get("role")
    if not user_id or not role:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    coll = {"admin": "admins", "teacher": "teachers", "student": "students"}.get(role, "students")
    user = await db[coll].find_one({"id": user_id}, {"_id": 0, "password_hash": 0})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    user["role"] = role
    return user


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") not in ("admin", "teacher"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def require_admin_only(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def require_student(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "student":
        raise HTTPException(status_code=403, detail="Student access required")
    return user


def clean_doc(doc: dict) -> dict:
    """Strip mongo _id and password_hash from a document."""
    if not doc:
        return doc
    doc.pop("_id", None)
    doc.pop("password_hash", None)
    doc.pop("collection_name", None)
    return doc


async def seed_initial_data() -> None:
    """Create default admin, demo student, and institute settings in MongoDB."""
    if db is None:
        logger.warning("Skipping initial seed because MongoDB is unavailable")
        return

    try:
        await db.ensure_indexes()
        existing = await db.institute_settings.find_one({"id": "default"})
        if not existing:
            await db.institute_settings.insert_one({
                "id": "default",
                "name": "Gyansai Maths IIT Center",
                "tagline": "Where Numbers Meet Destiny",
                "logo_url": "",
                "favicon_url": "",
                "address": "Plot 14, Education Lane, Pune, Maharashtra 411001",
                "contact_number": "+91 98765 43210",
                "email": "info@gyansai.com",
                "website": "https://gyansai.com",
                "upi_id": "gyansai@upi",
                "bank_account": "1234567890",
                "bank_ifsc": "HDFC0001234",
                "bank_name": "HDFC Bank",
                "social": {"youtube": "", "instagram": "", "twitter": "", "facebook": ""},
                "theme_primary": "#002FA7",
                "seo_title": "Gyansai Maths IIT Center — JEE/NEET/MHT-CET Test Portal",
                "seo_description": "Online examination & learning platform for JEE Main, JEE Advanced, NEET and MHT-CET aspirants.",
                "ga_id": "",
                "updated_at": iso(now_utc()),
            })

        admin = await db.admins.find_one({"email": "admin@gyansai"})
        if not admin:
            await db.admins.insert_one({
                "id": new_id(),
                "name": "Super Admin",
                "email": "admin@gyansai",
                "password_hash": hash_password("admin123"),
                "two_fa_enabled": False,
                "created_at": iso(now_utc()),
            })

        student = await db.students.find_one({"username": "demo"})
        if not student:
            await db.students.insert_one({
                "id": new_id(),
                "name": "Demo Student",
                "username": "demo",
                "password_hash": hash_password("demo123"),
                "email": "demo@gyansai.com",
                "mobile": "+91 90000 00000",
                "enrollment_no": "GS2026001",
                "photo_url": "",
                "status": "active",
                "course_ids": [],
                "exam_ids": [],
                "created_at": iso(now_utc()),
            })
    except Exception as exc:
        logger.warning("Initial seed skipped because MongoDB is unavailable: %s", exc)
