from fastapi import FastAPI, Depends, HTTPException, status, WebSocket, WebSocketDisconnect
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
import asyncio
import httpx
import json
import simulation_engine as sim

# CONFIG
SECRET_KEY = "SUPER_SECRET_KEY"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

DATABASE_URL = os.getenv("DATABASE_URL")

# IA / OpenAI (clave sólo por variable de entorno, nunca en código)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Pesos del modelo de prioridad multi-factor
PRIORITY_WEIGHTS = {
    "impacto_ciudadano": 0.35,
    "urgencia_temporal": 0.25,
    "riesgo_seguridad": 0.20,
    "vulnerabilidad_poblacion": 0.10,
    "reincidencia_probable": 0.10,
}

DEFAULT_PRIORITY_FACTORS = {
    "impacto_ciudadano": 50,
    "urgencia_temporal": 50,
    "riesgo_seguridad": 50,
    "vulnerabilidad_poblacion": 50,
    "reincidencia_probable": 50,
}

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"sslmode": "require"}  # IMPORTANTE para Render Postgres
)

SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

app = FastAPI()

# ─── Start simulation engine on startup ──────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    sim.start_simulation(asyncio.get_event_loop())

# ─── Fleet WebSocket ──────────────────────────────────────────────────────────

@app.websocket("/ws/fleet")
async def fleet_ws(websocket: WebSocket):
    await websocket.accept()
    sim.register_ws(websocket)
    # Send current state immediately on connect
    await websocket.send_text(__import__("json").dumps(sim.get_current_state()))
    try:
        while True:
            # Keep the connection alive; actual pushes come from the simulation loop
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        sim.unregister_ws(websocket)

# ─── Fleet HTTP polling fallback ──────────────────────────────────────────────

@app.get("/api/fleet/state")
def fleet_state():
    return sim.get_current_state()

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
    # Multi-factor metrics y pesos de prioridad (almacenados como JSON serializado)
    metrics_json = Column(Text, nullable=True)
    priority_weights = Column(Text, nullable=True)

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

# 🔥 HASH SIN PASSLIB

def hash_password(password: str):
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")

def verify_password(plain_password: str, hashed_password: str):
    return bcrypt.checkpw(
        plain_password.encode("utf-8"),
        hashed_password.encode("utf-8")
    )

# 🔥 CORREGIDO: JWT sub debe ser string
def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# 🔥 CORREGIDO: Convertir sub a int después de decodificar
def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id_str: str = payload.get("sub")
        if user_id_str is None:
            raise HTTPException(status_code=401, detail="Invalid token: missing sub")
        
        # Convertir el user_id de string a int
        user_id = int(user_id_str)
        
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=401, detail="Invalid token: user not found")
        return user
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid token: invalid user ID format")
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Token validation error: {str(e)}")

# MOTOR DE CLASIFICACIÓN (heurístico base, usado también como fallback si no hay IA)

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


# ─── IA (OpenAI) centralizada en backend ──────────────────────────────────────

def _openai_available() -> bool:
    return bool(OPENAI_API_KEY)


def _openai_chat(messages, max_tokens: int = 60) -> str:
    """
    Llamada central a OpenAI Chat Completions.
    La API key se lee exclusivamente de la variable de entorno OPENAI_API_KEY.
    """
    if not _openai_available():
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY no está configurada en el backend")

    try:
        response = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENAI_MODEL,
                "max_tokens": max_tokens,
                "messages": messages,
            },
            timeout=20.0,
        )
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Error conectando a OpenAI: {str(e)}")

    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Error OpenAI: {response.status_code} {response.text}")

    data = response.json()
    content = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
        .strip()
    )
    if not content:
        raise HTTPException(status_code=502, detail="Respuesta vacía de OpenAI")
    return content


def classify_ticket_with_ai(title: str, description: str) -> str:
    """
    Clasifica el área del ticket usando IA si OPENAI_API_KEY está configurada.
    Si no hay IA disponible, usa el motor heurístico existente.
    """
    if not _openai_available():
        area, _ = classify_ticket(description)
        return area

    content = _openai_chat(
        [
            {
                "role": "system",
                "content": (
                    "Eres un clasificador de solicitudes municipales. "
                    "Según el título y la descripción, responde SOLO el nombre del área más adecuada "
                    "entre opciones típicas como: \"Áreas Verdes\", \"Aseo\", \"Infraestructura\", "
                    "\"Atención General\" u otra similar, sin explicación adicional."
                ),
            },
            {
                "role": "user",
                "content": f"Título: {title}\nDescripción: {description}\nDevuelve solo el nombre del área.",
            },
        ],
        max_tokens=40,
    )

    area_name = content.splitlines()[0].strip()
    return area_name


def calculate_priority_factors_with_ai(title: str, description: str) -> dict:
    """
    Calcula factores de prioridad multi-factor usando IA si OPENAI_API_KEY está configurada.
    Devuelve un dict con cinco factores (0–100). Si la IA no está disponible o falla la llamada,
    retorna factores por defecto (todos = 50). Si la respuesta de IA no es JSON válido, lanza 502.
    """
    # Sin API key -> factores por defecto
    if not _openai_available():
        return DEFAULT_PRIORITY_FACTORS.copy()

    messages = [
        {
            "role": "system",
            "content": (
                "You are a municipal priority evaluation engine.\n"
                "Return ONLY valid JSON with numeric fields (0-100 integers) and no additional text."
            ),
        },
        {
            "role": "user",
            "content": (
                "Evaluate this municipal report and return:\n\n"
                "{\n"
                '  "impacto_ciudadano": number,\n'
                '  "urgencia_temporal": number,\n'
                '  "riesgo_seguridad": number,\n'
                '  "vulnerabilidad_poblacion": number,\n'
                '  "reincidencia_probable": number\n'
                "}\n\n"
                f"Title: {title}\n"
                f"Description: {description}"
            ),
        },
    ]

    try:
        raw = _openai_chat(messages, max_tokens=200)
    except HTTPException:
        # Fallo llamando a OpenAI (timeout, 5xx, etc.) -> factores por defecto
        return DEFAULT_PRIORITY_FACTORS.copy()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Respuesta no es JSON válido
        raise HTTPException(status_code=502, detail="Respuesta de OpenAI no es JSON válido para factores de prioridad")

    expected_keys = [
        "impacto_ciudadano",
        "urgencia_temporal",
        "riesgo_seguridad",
        "vulnerabilidad_poblacion",
        "reincidencia_probable",
    ]

    factors: dict[str, int] = {}
    for key in expected_keys:
        if key not in data:
            raise HTTPException(status_code=502, detail=f"Falta el campo '{key}' en la respuesta de OpenAI")
        value = data[key]
        # Aceptamos int/float/str que se pueda convertir a entero
        try:
            ivalue = int(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=502, detail=f"El campo '{key}' no es un entero válido: {value!r}")
        if not (0 <= ivalue <= 100):
            raise HTTPException(status_code=502, detail=f"El campo '{key}' está fuera de rango 0–100: {ivalue}")
        factors[key] = ivalue

    return factors


def compute_priority_score_from_factors(factors: dict, weights: dict) -> int:
    """
    Calcula el priority_score ponderado (0–100) a partir de factores y pesos.
    """
    total = 0.0
    for key, weight in weights.items():
        total += float(factors.get(key, 0)) * float(weight)
    score = round(total)
    return max(0, min(100, score))

# SCHEMAS

class UserCreate(BaseModel):
    name: str
    email: str
    password: str
    role: str

class TicketCreate(BaseModel):
    title: str
    description: str
    # Foto (máx 1) integrada a la solicitud. Puede ser URL o DataURL (base64).
    image_url: str | None = None
    image_description: str | None = ""


class AITicketPayload(BaseModel):
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

# 🔥 CORREGIDO: Convertir user.id a string en el token
@app.post("/login")
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):

    user = db.query(User).filter(User.email == form_data.username).first()

    if not user or not verify_password(form_data.password, user.password):
        raise HTTPException(status_code=400, detail="Incorrect credentials")

    # 🔥 IMPORTANTE: Convertir user.id a string para JWT
    token = create_access_token({"sub": str(user.id)})

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

    # Clasificación de área usando IA si está disponible (fallback heurístico)
    area_name = classify_ticket_with_ai(ticket.title, ticket.description)
    area = db.query(Area).filter(Area.name == area_name).first()

    if not area:
        area = Area(name=area_name, sla_hours=72)
        db.add(area)
        db.commit()
        db.refresh(area)

    # Factores de prioridad multi-factor (IA o valores por defecto)
    factors = calculate_priority_factors_with_ai(ticket.title, ticket.description)

    # Cálculo de priority_score ponderado
    priority_score = compute_priority_score_from_factors(factors, PRIORITY_WEIGHTS)

    # Urgencia derivada del score final
    urgency = calculate_urgency(priority_score)
    planned_date = datetime.utcnow() + timedelta(hours=area.sla_hours)

    new_ticket = Ticket(
        title=ticket.title,
        description=ticket.description,
        priority_score=priority_score,
        urgency_level=urgency,
        status="Recibido",
        planned_date=planned_date,
        area_id=area.id,
        user_id=current_user.id,
        metrics_json=json.dumps(factors),
        priority_weights=json.dumps(PRIORITY_WEIGHTS),
    )

    db.add(new_ticket)
    db.commit()
    db.refresh(new_ticket)

    # ─── Evidencia (máx 1 por ticket) ─────────────────────────────────────────
    evidence_id = None
    if ticket.image_url:
        existing_count = db.query(Evidence).filter(Evidence.ticket_id == new_ticket.id).count()
        if existing_count >= 1:
            raise HTTPException(status_code=400, detail="Este ticket ya tiene una foto asociada")

        ev = Evidence(
            ticket_id=new_ticket.id,
            image_url=ticket.image_url,
            description=(ticket.image_description or "")
        )
        db.add(ev)
        db.commit()
        db.refresh(ev)
        evidence_id = ev.id

    return {
        "ticket_id": new_ticket.id,
        "area": area.name,
        "priority": priority_score,
        "urgency_level": urgency,
        "planned_date": planned_date,
        "evidence_id": evidence_id,
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
        reporter = db.query(User).filter(User.id == ticket.user_id).first()
        evidences = db.query(Evidence).filter(Evidence.ticket_id == ticket.id).all()
        
        result.append({
            "id": ticket.id,
            "title": ticket.title,
            "description": ticket.description,
            "status": ticket.status,
            "urgency_level": ticket.urgency_level,
            "area_name": area.name if area else "Sin asignar",
            "assigned_to": assigned_user.name if assigned_user else None,
            "reported_by": reporter.name if reporter else None,
            "reported_by_email": reporter.email if reporter else None,
            "created_at": ticket.created_at,
            "planned_date": ticket.planned_date,
            "evidences": [
                {
                    "image_url": ev.image_url,
                    "description": getattr(ev, "description", ""),
                    "created_at": ev.created_at
                }
                for ev in evidences
            ],
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

    # Máx 1 foto por ticket
    existing_count = db.query(Evidence).filter(Evidence.ticket_id == ticket_id).count()
    if existing_count >= 1:
        raise HTTPException(status_code=400, detail="Este ticket ya tiene una foto asociada")

    evidence = Evidence(
        ticket_id=ticket_id, 
        image_url=request.image_url,
        description=request.description
    )
    db.add(evidence)
    db.commit()

    return {"message": "Evidence added", "evidence_id": evidence.id}


# ─── ENDPOINTS IA para frontend (monitor operador) ────────────────────────────

@app.post("/ai/tickets/classify")
def ai_classify_ticket(
    payload: AITicketPayload,
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in ["operador", "operator", "supervisor"]:
        raise HTTPException(status_code=403, detail="Solo operadores pueden acceder a IA de clasificación")

    # Área por IA (o heurística) + factores multi-factor para score
    area = classify_ticket_with_ai(payload.title, payload.description)
    factors = calculate_priority_factors_with_ai(payload.title, payload.description)
    score = compute_priority_score_from_factors(factors, PRIORITY_WEIGHTS)
    urgency = calculate_urgency(score)
    return {
        "area": area,
        "score": score,
        "urgency": urgency,
        "metrics": factors,
        "weights": PRIORITY_WEIGHTS,
    }


@app.post("/ai/tickets/priority")
def ai_ticket_priority(
    payload: AITicketPayload,
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in ["operador", "operator", "supervisor"]:
        raise HTTPException(status_code=403, detail="Solo operadores pueden acceder a IA de prioridad")

    # Factores multi-factor (IA o valores por defecto) para calcular priority_score
    factors = calculate_priority_factors_with_ai(payload.title, payload.description)
    score = compute_priority_score_from_factors(factors, PRIORITY_WEIGHTS)
    urgency = calculate_urgency(score)
    return {
        "score": score,
        "urgency": urgency,
        "metrics": factors,
        "weights": PRIORITY_WEIGHTS,
    }
