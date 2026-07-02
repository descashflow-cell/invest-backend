from dotenv import load_dotenv
from pathlib import Path
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

from fastapi import FastAPI, APIRouter, HTTPException, Depends, Request
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os, re, uuid, logging
from pydantic import BaseModel, Field, EmailStr
from typing import List, Optional
from datetime import datetime, timezone, timedelta
import bcrypt
import jwt

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

JWT_SECRET = os.environ['JWT_SECRET']
JWT_ALGO = "HS256"
JWT_TTL_HOURS = 24 * 7

app = FastAPI()
api_router = APIRouter(prefix="/api")

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
def _validate_month(month: str) -> str:
    if not MONTH_RE.match(month):
        raise HTTPException(status_code=400, detail="Mese non valido. Formato YYYY-MM")
    return month

# ----- Auth helpers -----
def hash_pwd(p: str) -> str:
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()

def verify_pwd(p: str, h: str) -> bool:
    try: return bcrypt.checkpw(p.encode(), h.encode())
    except Exception: return False

def create_token(user_id: str, email: str) -> str:
    payload = {"sub": user_id, "email": email,
               "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_TTL_HOURS)}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)

async def get_current_user(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth[7:]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0, "password_hash": 0})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user

# ----- Models -----
class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=100)
    name: Optional[str] = None

class LoginIn(BaseModel):
    email: EmailStr
    password: str

class UserOut(BaseModel):
    id: str
    email: str
    name: Optional[str] = None

class AuthOut(BaseModel):
    token: str
    user: UserOut

class SalaryIn(BaseModel):
    amount: float = Field(ge=0)

class SalaryOut(BaseModel):
    month: str; amount: float

class FixedExpenseIn(BaseModel):
    name: str = Field(min_length=1, max_length=120); amount: float = Field(ge=0)

class FixedExpense(BaseModel):
    id: str; name: str; amount: float; created_at: str

class ExtraExpenseIn(BaseModel):
    name: str = Field(min_length=1, max_length=120); amount: float = Field(ge=0)
    category: str = Field(min_length=1, max_length=60); month: str

class ExtraExpense(BaseModel):
    id: str; name: str; amount: float; category: str; month: str; created_at: str

class InvestmentIn(BaseModel):
    name: str = Field(min_length=1, max_length=80); amount: float = Field(gt=0); month: str; type: str = Field(min_length=1, max_length=20)

class Investment(BaseModel):
    id: str; name: str; amount: float; month: str; type: str; created_at: str

# ----- Auth routes -----
@api_router.post("/auth/register", response_model=AuthOut)
async def register(payload: RegisterIn):
    email = payload.email.lower().strip()
    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=400, detail="Email already registered")
    uid = str(uuid.uuid4())
    user = {"id": uid, "email": email, "name": (payload.name or "").strip() or None,
            "password_hash": hash_pwd(payload.password),
            "created_at": datetime.now(timezone.utc).isoformat()}
    await db.users.insert_one(user)
    return {"token": create_token(uid, email),
            "user": {"id": uid, "email": email, "name": user["name"]}}

@api_router.post("/auth/login", response_model=AuthOut)
async def login(payload: LoginIn):
    email = payload.email.lower().strip()
    user = await db.users.find_one({"email": email})
    if not user or not verify_pwd(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    return {"token": create_token(user["id"], email),
            "user": {"id": user["id"], "email": email, "name": user.get("name")}}

@api_router.get("/auth/me", response_model=UserOut)
async def me(user: dict = Depends(get_current_user)):
    return {"id": user["id"], "email": user["email"], "name": user.get("name")}

# ----- Data routes (all protected, scoped by user_id) -----
@api_router.get("/salary/{month}", response_model=SalaryOut)
async def get_salary(month: str, user=Depends(get_current_user)):
    _validate_month(month)
    d = await db.salaries.find_one({"month": month, "user_id": user["id"]}, {"_id": 0})
    return {"month": month, "amount": float(d["amount"]) if d else 0.0}

@api_router.put("/salary/{month}", response_model=SalaryOut)
async def put_salary(month: str, payload: SalaryIn, user=Depends(get_current_user)):
    _validate_month(month)
    await db.salaries.update_one(
        {"month": month, "user_id": user["id"]},
        {"$set": {"month": month, "amount": payload.amount, "user_id": user["id"]}},
        upsert=True)
    return {"month": month, "amount": payload.amount}

@api_router.get("/fixed-expenses", response_model=List[FixedExpense])
async def list_fixed(user=Depends(get_current_user)):
    docs = await db.fixed_expenses.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).sort("created_at", 1).to_list(1000)
    return docs

@api_router.post("/fixed-expenses", response_model=FixedExpense)
async def add_fixed(payload: FixedExpenseIn, user=Depends(get_current_user)):
    item = {"id": str(uuid.uuid4()), "name": payload.name.strip(), "amount": payload.amount,
            "created_at": datetime.now(timezone.utc).isoformat(), "user_id": user["id"]}
    await db.fixed_expenses.insert_one(item)
    item.pop("user_id"); item.pop("_id", None)
    return item

@api_router.delete("/fixed-expenses/{iid}")
async def del_fixed(iid: str, user=Depends(get_current_user)):
    r = await db.fixed_expenses.delete_one({"id": iid, "user_id": user["id"]})
    if r.deleted_count == 0: raise HTTPException(404, "Not found")
    return {"ok": True}

@api_router.get("/extra-expenses", response_model=List[ExtraExpense])
async def list_extra(month: str, user=Depends(get_current_user)):
    _validate_month(month)
    docs = await db.extra_expenses.find({"month": month, "user_id": user["id"]}, {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(1000)
    return docs

@api_router.post("/extra-expenses", response_model=ExtraExpense)
async def add_extra(payload: ExtraExpenseIn, user=Depends(get_current_user)):
    _validate_month(payload.month)
    item = {"id": str(uuid.uuid4()), "name": payload.name.strip(), "amount": payload.amount,
            "category": payload.category.strip(), "month": payload.month,
            "created_at": datetime.now(timezone.utc).isoformat(), "user_id": user["id"]}
    await db.extra_expenses.insert_one(item)
    item.pop("user_id"); item.pop("_id", None)
    return item

@api_router.delete("/extra-expenses/{iid}")
async def del_extra(iid: str, user=Depends(get_current_user)):
    r = await db.extra_expenses.delete_one({"id": iid, "user_id": user["id"]})
    if r.deleted_count == 0: raise HTTPException(404, "Not found")
    return {"ok": True}

@api_router.get("/investments", response_model=List[Investment])
async def list_inv(month: str, user=Depends(get_current_user)):
    _validate_month(month)
    docs = await db.investments.find({"month": month, "user_id": user["id"]}, {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(1000)
    return docs

@api_router.post("/investments", response_model=Investment)
async def add_inv(payload: InvestmentIn, user=Depends(get_current_user)):
    _validate_month(payload.month)
    item = {"id": str(uuid.uuid4()), "name": payload.name.strip(), "amount": payload.amount,
            "month": payload.month, "type": payload.type.strip(), "created_at": datetime.now(timezone.utc).isoformat(),
            "user_id": user["id"]}
    await db.investments.insert_one(item)
    item.pop("user_id"); item.pop("_id", None)
    return item

@api_router.delete("/investments/{iid}")
async def del_inv(iid: str, user=Depends(get_current_user)):
    r = await db.investments.delete_one({"id": iid, "user_id": user["id"]})
    if r.deleted_count == 0: raise HTTPException(404, "Non trovato")
    return {"ok": True}

@api_router.get("/portfolio")
async def portfolio(user=Depends(get_current_user)):
    items = []; total = 0.0
    async for d in db.investments.aggregate([
        {"$match": {"user_id": user["id"]}},
        {"$group": {"_id": "$name", "total": {"$sum": "$amount"}}},
        {"$sort": {"total": -1}}]):
        items.append({"name": d["_id"], "total": float(d["total"])}); total += float(d["total"])
    starting_inv = await db.investments.find({"type": "initial", "user_id": user["id"]}, {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(1000)
    return {"total": total, "items": items, "starting_investments": starting_inv}

@api_router.get("/summary/{month}")
async def summary(month: str, user=Depends(get_current_user)):
    _validate_month(month)
    uid = user["id"]
    sd = await db.salaries.find_one({"month": month, "user_id": uid}, {"_id": 0})
    salary = float(sd["amount"]) if sd else 0.0
    fixed = await db.fixed_expenses.find({"user_id": uid}, {"_id": 0, "user_id": 0}).sort("created_at", 1).to_list(1000)
    fixed_total = sum(x["amount"] for x in fixed)
    extra = await db.extra_expenses.find({"month": month, "user_id": uid}, {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(1000)
    extra_total = sum(x["amount"] for x in extra)
    cat = {}
    for e in extra: cat[e["category"]] = cat.get(e["category"], 0.0) + e["amount"]
    by_category = [{"category": k, "total": v} for k, v in sorted(cat.items(), key=lambda kv: -kv[1])]
    inv = await db.investments.find({"month": month, "user_id": uid, "type": {"$ne": "initial"}}, {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(1000)
    inv_total = sum(x["amount"] for x in inv if x.get("type") != "extra")
    balance = salary - fixed_total - extra_total
    return {"month": month, "salary": salary, "fixed_total": fixed_total, "extra_total": extra_total,
            "balance": balance, "fixed_expenses": fixed, "extra_expenses": extra, "by_category": by_category,
            "investments_month": inv, "investments_month_total": inv_total,
            "suggested_investable": max(0.0, balance * 0.5)}

@api_router.get("/ytd/{year}")
async def ytd(year: int, user=Depends(get_current_user)):
    if year < 2000 or year > 2100: raise HTTPException(400, "Anno non valido")
    uid = user["id"]
    months = [f"{year}-{m:02d}" for m in range(1, 13)]
    salaries = {d["month"]: float(d["amount"]) async for d in db.salaries.find({"month": {"$in": months}, "user_id": uid}, {"_id": 0})}
    fixed_docs = await db.fixed_expenses.find({"user_id": uid}, {"_id": 0}).to_list(1000)
    fixed_total = sum(float(d["amount"]) for d in fixed_docs)
    extra_map = {}
    async for d in db.extra_expenses.aggregate([{"$match": {"month": {"$in": months}, "user_id": uid}}, {"$group": {"_id": "$month", "total": {"$sum": "$amount"}}}]):
        extra_map[d["_id"]] = float(d["total"])
    inv_map = {}
    async for d in db.investments.aggregate([{"$match": {"month": {"$in": months}, "user_id": uid, "type": {"$nin": ["initial", "extra"]}}}, {"$group": {"_id": "$month", "total": {"$sum": "$amount"}}}]):
        inv_map[d["_id"]] = float(d["total"])
    series = []; ti=tf=te=tin=ts=0.0; am=0; best=None; worst=None
    for m in months:
        s = salaries.get(m, 0.0); e = extra_map.get(m, 0.0); i_ = inv_map.get(m, 0.0)
        f = fixed_total if s > 0 else 0.0
        bal = s - f - e; saved = bal - i_; act = s>0 or e>0 or i_>0
        if act: am += 1
        series.append({"month": m, "income": s, "fixed": f, "extra": e, "expenses": f+e, "invested": i_, "balance": bal, "saved": saved, "active": act})
        ti+=s; tf+=f; te+=e; tin+=i_; ts+=saved
        if act:
            if best is None or saved > best["saved"]: best = {"month": m, "saved": saved}
            if worst is None or saved < worst["saved"]: worst = {"month": m, "saved": saved}
    return {"year": year, "series": series, "best_month": best, "worst_month": worst,
            "totals": {"income": ti, "fixed": tf, "extra": te, "expenses": tf+te, "invested": tin, "saved": ts, "active_months": am, "avg_saved": (ts/am) if am else 0.0}}

app.include_router(api_router)
app.add_middleware(CORSMiddleware, allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
async def startup():
    await db.users.create_index("email", unique=True)

@app.on_event("shutdown")
async def shutdown(): client.close()

logging.basicConfig(level=logging.INFO)
