from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from jose import jwt
from datetime import datetime, timedelta
from pydantic import BaseModel
from typing import Optional, List
import os
import bcrypt

# ─── CONFIG (leer desde variables de entorno) ────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

# FRONTEND_URL: en Render setear como variable de entorno con la URL de Vercel
# Ej: https://vita360.vercel.app
FRONTEND_URL = os.getenv("FRONTEND_URL", "*")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./vita360.db")

# Render entrega postgres:// pero SQLAlchemy necesita postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args={"sslmode": "require"})

SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

app = FastAPI(title="Vita360 API")

# ─── CORS ────────────────────────────────────────────────────────────────────
# Si FRONTEND_URL es "*" permite todo (dev). En prod setear la URL exacta de Vercel.
origins = ["*"] if FRONTEND_URL == "*" else [
    FRONTEND_URL,
    FRONTEND_URL.rstrip("/"),          # sin trailing slash
    "http://localhost:5173",           # dev local
    "http://localhost:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
    expose_headers=["*"],
    max_age=600,
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

# ─── MODELOS DB ───────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    email = Column(String, unique=True)
    password = Column(String)
    role = Column(String)  # "ciudadano" o "operador"

class Area(Base):
    __tablename__ = "areas"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    sla_hours = Column(Integer)

class Ticket(Base):
    __tablename__ = "tickets"
    id = Column(Integer, primary_key=True)
    title = Column(String)
    description = Column(Text)
    priority_score = Column(Integer)
    urgency_level = Column(String)
    status = Column(String)
    planned_date = Column(DateTime)
    area_id = Column(Integer, ForeignKey("areas.id"), nullable=True)
    area_name = Column(String, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    assigned_to = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Evidence(Base):
    __tablename__ = "evidence"
    id = Column(Integer, primary_key=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"))
    image_url = Column(Text)           # base64 o URL — Text para soportar base64 largo
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(engine)

# ─── UTILIDADES ───────────────────────────────────────────────────────────────

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())

def create_access_token(data: dict) -> str:
    payload = {**data, "exp": datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user = db.query(User).filter(User.id == payload.get("sub")).first()
        if not user:
            raise HTTPException(status_code=401, detail="Token inválido")
        return user
    except Exception:
        raise HTTPException(status_code=401, detail="Token inválido")

# ─── CLASIFICACIÓN IA ─────────────────────────────────────────────────────────

def classify_ticket(description: str):
    d = description.lower()
    if "arbol" in d or "árbol" in d:         return "Áreas Verdes", 90
    if "agua" in d or "alcantarilla" in d \
       or "inundacion" in d or "inundación" in d: return "Obras Sanitarias", 85
    if "vereda" in d or "hoyo" in d \
       or "bache" in d or "pavimento" in d:  return "Infraestructura", 80
    if "luz" in d or "alumbrado" in d \
       or "poste" in d or "foco" in d:       return "Alumbrado Público", 75
    if "basura" in d or "contenedor" in d \
       or "residuo" in d:                    return "Aseo", 70
    return "Atención General", 50

def calculate_urgency(score: int) -> str:
    return "Alta" if score >= 85 else "Media" if score >= 60 else "Baja"

# ─── SCHEMAS ──────────────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    name: str
    email: str
    password: str
    role: str  # "ciudadano" | "operador"

class TicketCreate(BaseModel):
    title: str
    description: str

class EvidenceCreate(BaseModel):
    image_url: str          # base64 data URL o https://...
    description: Optional[str] = None

class AssignTicket(BaseModel):
    assigned_to: str

class UpdateStatus(BaseModel):
    status: str

# ─── ENDPOINTS ────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "service": "Vita360 API"}

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.post("/register")
def register(user: UserCreate, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == user.email).first():
        raise HTTPException(status_code=400, detail="Email ya registrado")
    if user.role not in ["ciudadano", "operador"]:
        raise HTTPException(status_code=400, detail="Rol debe ser 'ciudadano' u 'operador'")
    db.add(User(name=user.name, email=user.email, password=hash_password(user.password), role=user.role))
    db.commit()
    return {"message": "Usuario creado"}

@app.post("/login")
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == form_data.username).first()
    if not user or not verify_password(form_data.password, user.password):
        raise HTTPException(status_code=400, detail="Credenciales incorrectas")
    return {
        "access_token": create_access_token({"sub": user.id}),
        "token_type": "bearer",
        "role": user.role,
        "name": user.name,
    }

@app.get("/me")
def me(current_user: User = Depends(get_current_user)):
    return {"id": current_user.id, "name": current_user.name, "email": current_user.email, "role": current_user.role}

# ── Ciudadano: crear ticket ───────────────────────────────────────────────────

@app.post("/tickets")
def create_ticket(ticket: TicketCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    area_name, score = classify_ticket(ticket.description)
    area = db.query(Area).filter(Area.name == area_name).first()
    if not area:
        area = Area(name=area_name, sla_hours=72)
        db.add(area); db.commit(); db.refresh(area)

    urgency = calculate_urgency(score)
    planned = datetime.utcnow() + timedelta(hours=area.sla_hours)

    t = Ticket(
        title=ticket.title, description=ticket.description,
        priority_score=score, urgency_level=urgency,
        status="Recibido", planned_date=planned,
        area_id=area.id, area_name=area.name, user_id=current_user.id
    )
    db.add(t); db.commit(); db.refresh(t)
    return {"ticket_id": t.id, "area": area.name, "priority": urgency, "planned_date": planned, "status": t.status}

# ── Ciudadano: ver sus tickets ────────────────────────────────────────────────

@app.get("/my-tickets")
def my_tickets(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tickets = db.query(Ticket).filter(Ticket.user_id == current_user.id).order_by(Ticket.created_at.desc()).all()
    return [
        {
            "id": t.id, "title": t.title, "description": t.description,
            "status": t.status, "urgency_level": t.urgency_level, "area_name": t.area_name,
            "assigned_to": t.assigned_to, "planned_date": t.planned_date, "created_at": t.created_at,
            "evidences": [
                {"image_url": e.image_url, "description": e.description, "created_at": e.created_at}
                for e in db.query(Evidence).filter(Evidence.ticket_id == t.id).all()
            ],
        }
        for t in tickets
    ]

# ── Ciudadano: subir evidencia/foto ──────────────────────────────────────────

@app.post("/tickets/{ticket_id}/evidence")
def add_evidence(ticket_id: int, evidence: EvidenceCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket no encontrado")
    if ticket.user_id != current_user.id and current_user.role not in ["operador", "operator", "supervisor"]:
        raise HTTPException(status_code=403, detail="Sin permisos")
    db.add(Evidence(ticket_id=ticket_id, image_url=evidence.image_url, description=evidence.description))
    db.commit()
    return {"message": "Evidencia agregada"}

# ── Operador: ver todos los tickets ──────────────────────────────────────────

@app.get("/tickets")
def get_tickets(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role not in ["operador", "operator", "supervisor"]:
        raise HTTPException(status_code=403, detail="Sin permisos")
    tickets = db.query(Ticket).order_by(Ticket.priority_score.desc()).all()
    return [
        {
            "id": t.id, "title": t.title, "description": t.description,
            "status": t.status, "urgency_level": t.urgency_level, "priority_score": t.priority_score,
            "area_name": t.area_name, "assigned_to": t.assigned_to,
            "planned_date": t.planned_date, "created_at": t.created_at,
            "reported_by": (u := db.query(User).filter(User.id == t.user_id).first()) and u.name or "Desconocido",
            "reported_by_email": u.email if u else "",
            "evidences": [
                {"image_url": e.image_url, "description": e.description, "created_at": e.created_at}
                for e in db.query(Evidence).filter(Evidence.ticket_id == t.id).all()
            ],
        }
        for t in tickets
    ]

# ── Operador: asignar equipo ──────────────────────────────────────────────────

@app.patch("/tickets/{ticket_id}/assign")
def assign_ticket(ticket_id: int, body: AssignTicket, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role not in ["operador", "operator", "supervisor"]:
        raise HTTPException(status_code=403, detail="Sin permisos")
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket no encontrado")
    ticket.assigned_to = body.assigned_to
    if ticket.status == "Recibido":
        ticket.status = "Asignado"
    db.commit()
    return {"message": "Equipo asignado", "status": ticket.status}

# ── Operador: cambiar estado ──────────────────────────────────────────────────

@app.patch("/tickets/{ticket_id}/status")
def update_status(ticket_id: int, body: UpdateStatus, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role not in ["operador", "operator", "supervisor"]:
        raise HTTPException(status_code=403, detail="Sin permisos")
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket no encontrado")
    VALID = ["Recibido", "Asignado", "En Gestión", "Resuelto", "Cerrado"]
    if body.status not in VALID:
        raise HTTPException(status_code=400, detail=f"Estado inválido. Opciones: {VALID}")
    ticket.status = body.status
    db.commit()
    return {"message": "Estado actualizado", "status": ticket.status}
