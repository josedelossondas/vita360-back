from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship, Session
from jose import JWTError, jwt
from datetime import datetime, timedelta
from pydantic import BaseModel
import os
import bcrypt

# CONFIG
SECRET_KEY = "SUPER_SECRET_KEY"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

DATABASE_URL = os.getenv("DATABASE_URL")

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"sslmode": "require"}  # IMPORTANTE para Render Postgres
)

SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

app = FastAPI()

# CORS - Permitir frontend en cualquier origen
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # En producción: ["https://vita360.vercel.app"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

# MODELOS DB

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    email = Column(String, unique=True)
    password = Column(String)
    role = Column(String)

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
    area_id = Column(Integer, ForeignKey("areas.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    assigned_to = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Evidence(Base):
    __tablename__ = "evidence"
    id = Column(Integer, primary_key=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"))
    image_url = Column(String)
    description = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(engine)

# UTILIDADES

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# 🔥 NUEVO HASH SIN PASSLIB

def hash_password(password: str):
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")

def verify_password(plain_password: str, hashed_password: str):
    return bcrypt.checkpw(
        plain_password.encode("utf-8"),
        hashed_password.encode("utf-8")
    )

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: int = payload.get("sub")
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=401, detail="Invalid token")
        return user
    except:
        raise HTTPException(status_code=401, detail="Invalid token")

# MOTOR DE CLASIFICACIÓN

def classify_ticket(description):
    description = description.lower()

    if "árbol" in description:
        return "Áreas Verdes", 90
    if "basura" in description or "contenedor" in description:
        return "Aseo", 70
    if "vereda" in description or "hoyo" in description:
        return "Infraestructura", 80

    return "Atención General", 50

def calculate_urgency(score):
    if score >= 85:
        return "Alta"
    if score >= 60:
        return "Media"
    return "Baja"

# SCHEMAS

class UserCreate(BaseModel):
    name: str
    email: str
    password: str
    role: str

class TicketCreate(BaseModel):
    title: str
    description: str

# ENDPOINTS

@app.post("/register")
def register(user: UserCreate, db: Session = Depends(get_db)):

    existing = db.query(User).filter(User.email == user.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    hashed = hash_password(user.password)

    new_user = User(
        name=user.name,
        email=user.email,
        password=hashed,
        role=user.role
    )

    db.add(new_user)
    db.commit()

    return {"message": "User created"}

@app.post("/login")
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):

    user = db.query(User).filter(User.email == form_data.username).first()

    if not user or not verify_password(form_data.password, user.password):
        raise HTTPException(status_code=400, detail="Incorrect credentials")

    token = create_access_token({"sub": user.id})

    return {
        "access_token": token, 
        "token_type": "bearer",
        "role": user.role,
        "name": user.name,
        "id": user.id
    }

@app.post("/tickets")
def create_ticket(
    ticket: TicketCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):

    area_name, score = classify_ticket(ticket.description)
    area = db.query(Area).filter(Area.name == area_name).first()

    if not area:
        area = Area(name=area_name, sla_hours=72)
        db.add(area)
        db.commit()
        db.refresh(area)

    urgency = calculate_urgency(score)
    planned_date = datetime.utcnow() + timedelta(hours=area.sla_hours)

    new_ticket = Ticket(
        title=ticket.title,
        description=ticket.description,
        priority_score=score,
        urgency_level=urgency,
        status="Recibido",
        planned_date=planned_date,
        area_id=area.id,
        user_id=current_user.id
    )

    db.add(new_ticket)
    db.commit()
    db.refresh(new_ticket)

    return {
        "ticket_id": new_ticket.id,
        "area": area.name,
        "priority": urgency,
        "planned_date": planned_date
    }

@app.get("/my-tickets")
def my_tickets(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tickets = db.query(Ticket).filter(Ticket.user_id == current_user.id).all()
    
    result = []
    for ticket in tickets:
        area = db.query(Area).filter(Area.id == ticket.area_id).first()
        assigned_user = db.query(User).filter(User.id == ticket.assigned_to).first() if ticket.assigned_to else None
        evidences = db.query(Evidence).filter(Evidence.ticket_id == ticket.id).all()
        
        result.append({
            "id": ticket.id,
            "title": ticket.title,
            "description": ticket.description,
            "status": ticket.status,
            "urgency_level": ticket.urgency_level,
            "area_name": area.name if area else "Sin asignar",
            "assigned_to": assigned_user.name if assigned_user else None,
            "created_at": ticket.created_at,
            "planned_date": ticket.planned_date,
            "evidences": [
                {
                    "image_url": ev.image_url,
                    "description": getattr(ev, "description", ""),
                    "created_at": ev.created_at
                }
                for ev in evidences
            ]
        })
    
    return result

@app.get("/tickets")
def get_tickets(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role not in ["operador", "operator", "supervisor"]:
        raise HTTPException(status_code=403, detail="Solo operadores pueden acceder")

    tickets = db.query(Ticket).order_by(Ticket.priority_score.desc()).all()
    
    result = []
    for ticket in tickets:
        area = db.query(Area).filter(Area.id == ticket.area_id).first()
        assigned_user = db.query(User).filter(User.id == ticket.assigned_to).first() if ticket.assigned_to else None
        
        result.append({
            "id": ticket.id,
            "title": ticket.title,
            "description": ticket.description,
            "status": ticket.status,
            "urgency_level": ticket.urgency_level,
            "area_name": area.name if area else "Sin asignar",
            "assigned_to": assigned_user.name if assigned_user else None,
            "created_at": ticket.created_at,
            "planned_date": ticket.planned_date,
        })
    
    return result

class UpdateStatusRequest(BaseModel):
    status: str

@app.patch("/tickets/{ticket_id}/status")
def update_status(
    ticket_id: int, 
    request: UpdateStatusRequest,
    current_user: User = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    ticket.status = request.status
    db.commit()

    return {"message": "Status updated", "new_status": request.status}

class AddEvidenceRequest(BaseModel):
    image_url: str
    description: str = ""

@app.post("/tickets/{ticket_id}/evidence")
def add_evidence(
    ticket_id: int, 
    request: AddEvidenceRequest,
    current_user: User = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    evidence = Evidence(
        ticket_id=ticket_id, 
        image_url=request.image_url,
        description=request.description
    )
    db.add(evidence)
    db.commit()

    return {"message": "Evidence added", "evidence_id": evidence.id}