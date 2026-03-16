from fastapi import FastAPI, Depends, HTTPException, status, WebSocket, WebSocketDisconnect
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, DateTime, Text, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship, Session
from jose import JWTError, jwt
from datetime import datetime, timedelta
from pydantic import BaseModel
from typing import Optional, List
import os
import bcrypt
import asyncio
import httpx
import json
import random
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

# CORS - DEBE ir ANTES de cualquier ruta para que los errores 4xx/5xx
# también incluyan las headers Access-Control-Allow-Origin
# En producción (Vercel) evitamos "*" para mayor compatibilidad con navegadores,
# preflight y futuras credenciales/cookies.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://vita360.vercel.app",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

class Squad(Base):
    __tablename__ = "squads"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    area_name = Column(String, nullable=True)
    pending_tasks = Column(Integer, default=0)
    squad_type = Column(String, default="cuadrilla", nullable=True)

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
    squad_name = Column(String, nullable=True)           # ← nuevo: nombre cuadrilla asignada
    created_at = Column(DateTime, default=datetime.utcnow)
    # Geolocalización
    lat = Column(Float, nullable=True)                   # ← nuevo
    lng = Column(Float, nullable=True)                   # ← nuevo
    # Datos generados por IA de tarea
    task_summary = Column(String, nullable=True)         # ← nuevo: descripción de la tarea
    estimated_hours = Column(Integer, nullable=True)     # ← nuevo: horas estimadas de resolución
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
        user_id_str: str = payload.get("sub")
        if user_id_str is None:
            raise HTTPException(status_code=401, detail="Invalid token: missing sub")
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


# ─── IA (OpenAI) centralizada en backend ──────────────────────────────────────

def _openai_available() -> bool:
    return bool(OPENAI_API_KEY)


def _openai_chat(messages, max_tokens: int = 60) -> str:
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
        return DEFAULT_PRIORITY_FACTORS.copy()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Respuesta de OpenAI no es JSON válido para factores de prioridad")

    expected_keys = [
        "impacto_ciudadano",
        "urgencia_temporal",
        "riesgo_seguridad",
        "vulnerabilidad_poblacion",
        "reincidencia_probable",
    ]

    factors: dict = {}
    for key in expected_keys:
        if key not in data:
            raise HTTPException(status_code=502, detail=f"Falta el campo '{key}' en la respuesta de OpenAI")
        value = data[key]
        try:
            ivalue = int(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=502, detail=f"El campo '{key}' no es un entero válido: {value!r}")
        if not (0 <= ivalue <= 100):
            raise HTTPException(status_code=502, detail=f"El campo '{key}' está fuera de rango 0–100: {ivalue}")
        factors[key] = ivalue

    return factors


# ─── Vitacura polygon helpers ─────────────────────────────────────────────────

# Polígono de la comuna de Vitacura (lon, lat) — actualizado con GeoJSON oficial
VITACURA_POLYGON = [
    (-70.60720212276362, -33.40979121627189),
    (-70.60840916155004, -33.40062873415507),
    (-70.60289165587551, -33.389499332962856),
    (-70.60089636010098, -33.384992599105885),
    (-70.60277694816887, -33.38235000758301),
    (-70.60036296220976, -33.3760046376323),
    (-70.59916175951844, -33.36285230108139),
    (-70.5857895350789,  -33.353903448919226),
    (-70.57726401140697, -33.3514840846016),
    (-70.56659382227234, -33.35481947183783),
    (-70.55266896741463, -33.358284938330605),
    (-70.54445398028918, -33.366885883318275),
    (-70.53934955826718, -33.37061534300281),
    (-70.52074458758153, -33.36750323758047),
    (-70.51749157581536, -33.37295876607694),
    (-70.5258190410902,  -33.37655552632404),
    (-70.53453836833025, -33.38463659966653),
    (-70.58657798777993, -33.4050878306965),
    (-70.60055765154465, -33.409295179207504),
    (-70.60727829770096, -33.4098027342863),
    # cierre del polígono
    (-70.60720212276362, -33.40979121627189),
]

def _point_in_polygon(x: float, y: float, poly: list) -> bool:
    """Ray-casting algorithm: returns True if point (x,y) is inside polygon."""
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside

def _random_point_in_vitacura() -> tuple:
    """Return a random (lat, lng) strictly inside the Vitacura polygon."""
    lons = [p[0] for p in VITACURA_POLYGON]
    lats = [p[1] for p in VITACURA_POLYGON]
    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)
    for _ in range(1000):
        lon = random.uniform(min_lon, max_lon)
        lat = random.uniform(min_lat, max_lat)
        if _point_in_polygon(lon, lat, VITACURA_POLYGON):
            return lat, lon
    # Fallback: centroid del nuevo polígono de Vitacura
    return -33.3850, -70.5660


def compute_priority_score_from_factors(factors: dict, weights: dict) -> int:
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
    # Foto integrada a la solicitud
    image_url: Optional[str] = None
    image_description: Optional[str] = ""
    # Geolocalización enviada por el frontend
    lat: Optional[float] = None
    lng: Optional[float] = None
    timestamp: Optional[str] = None

class TicketUpdate(BaseModel):
    status: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None

class AITicketPayload(BaseModel):
    title: str
    description: str

class AssignSquadRequest(BaseModel):
    squad_name: str
    estimated_hours: Optional[int] = None  # ← horas estimadas; si no viene, se lee del ticket

class UpdateStatusRequest(BaseModel):
    status: str

class AddEvidenceRequest(BaseModel):
    image_url: str
    description: str = ""

# ─── Helper: serializar ticket ────────────────────────────────────────────────

def _serialize_ticket(ticket: Ticket, db: Session, include_reporter: bool = False) -> dict:
    area = db.query(Area).filter(Area.id == ticket.area_id).first()
    assigned_user = db.query(User).filter(User.id == ticket.assigned_to).first() if ticket.assigned_to else None
    evidences = db.query(Evidence).filter(Evidence.ticket_id == ticket.id).all()

    result = {
        "id": ticket.id,
        "title": ticket.title,
        "description": ticket.description,
        "status": ticket.status,
        "urgency_level": ticket.urgency_level,
        "priority_score": ticket.priority_score,
        "area_name": area.name if area else "Sin asignar",
        "squad_name": ticket.squad_name,
        "task_summary": ticket.task_summary,           # ← nuevo
        "estimated_hours": ticket.estimated_hours,     # ← nuevo
        "assigned_to": assigned_user.name if assigned_user else None,
        "created_at": ticket.created_at,
        "planned_date": ticket.planned_date,
        "lat": ticket.lat,
        "lon": ticket.lng,   # el frontend espera 'lon' no 'lng'
        "evidences": [
            {
                "image_url": ev.image_url,
                "description": getattr(ev, "description", ""),
                "created_at": ev.created_at,
            }
            for ev in evidences
        ],
    }

    if include_reporter:
        reporter = db.query(User).filter(User.id == ticket.user_id).first()
        result["reported_by"] = reporter.name if reporter else None
        result["reported_by_email"] = reporter.email if reporter else None

    return result

# ENDPOINTS

@app.post("/register")
def register(user: UserCreate, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == user.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    hashed = hash_password(user.password)
    new_user = User(name=user.name, email=user.email, password=hashed, role=user.role)
    db.add(new_user)
    db.commit()
    return {"message": "User created"}

@app.post("/login")
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == form_data.username).first()
    if not user or not verify_password(form_data.password, user.password):
        raise HTTPException(status_code=400, detail="Incorrect credentials")

    token = create_access_token({"sub": str(user.id)})
    return {
        "access_token": token,
        "token_type": "bearer",
        "role": user.role,
        "name": user.name,
        "id": user.id,
    }

# ─── TICKETS ──────────────────────────────────────────────────────────────────

@app.post("/tickets")
def create_ticket(
    ticket: TicketCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    area_name = classify_ticket_with_ai(ticket.title, ticket.description)
    area = db.query(Area).filter(Area.name == area_name).first()
    if not area:
        area = Area(name=area_name, sla_hours=72)
        db.add(area)
        db.commit()
        db.refresh(area)

    factors = calculate_priority_factors_with_ai(ticket.title, ticket.description)
    priority_score = compute_priority_score_from_factors(factors, PRIORITY_WEIGHTS)
    urgency = calculate_urgency(priority_score)
    planned_date = datetime.utcnow() + timedelta(hours=area.sla_hours)

    # Usar coordenadas del ciudadano o generar punto aleatorio dentro de Vitacura
    if ticket.lat is not None and ticket.lng is not None:
        ticket_lat, ticket_lng = ticket.lat, ticket.lng
    else:
        ticket_lat, ticket_lng = _random_point_in_vitacura()

    new_ticket = Ticket(
        title=ticket.title,
        description=ticket.description,
        priority_score=priority_score,
        urgency_level=urgency,
        status="Recibido",
        planned_date=planned_date,
        area_id=area.id,
        user_id=current_user.id,
        lat=ticket_lat,
        lng=ticket_lng,
        metrics_json=json.dumps(factors),
        priority_weights=json.dumps(PRIORITY_WEIGHTS),
    )

    db.add(new_ticket)
    db.commit()
    db.refresh(new_ticket)

    # ─── Evidencia ────────────────────────────────────────────────────────────
    evidence_id = None
    if ticket.image_url:
        ev = Evidence(
            ticket_id=new_ticket.id,
            image_url=ticket.image_url,
            description=(ticket.image_description or ""),
        )
        db.add(ev)
        db.commit()
        db.refresh(ev)
        evidence_id = ev.id

    return {
        "id": new_ticket.id,
        "ticket_id": new_ticket.id,
        "area": area.name,
        "priority": priority_score,
        "urgency_level": urgency,
        "planned_date": planned_date,
        "evidence_id": evidence_id,
        "metrics": {
            "riesgo_seguridad": factors.get("riesgo_seguridad", 50),
            "urgencia": factors.get("urgencia_temporal", 50),
            "impacto": factors.get("impacto_ciudadano", 50),
            "sla_legal": 50,
            "vulnerabilidad_lugar": factors.get("vulnerabilidad_poblacion", 50),
        },
        "location_context": {
            "near_school": False,
            "near_hospital": False,
            "near_high_traffic": False,
            "in_critical_zone": False,
        },
        "reasoning": f"Ticket clasificado en área '{area.name}' con prioridad {urgency} (score {priority_score}/100).",
    }

@app.get("/my-tickets")
def my_tickets(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tickets = db.query(Ticket).filter(Ticket.user_id == current_user.id).all()
    return [_serialize_ticket(t, db) for t in tickets]

@app.get("/tickets/count")
def get_tickets_count(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Endpoint ligero para el monitor de IA.
    Devuelve solo el total de tickets sin serializar nada.
    Solo accesible por operadores/supervisores."""
    if current_user.role not in ["operador", "operator", "supervisor"]:
        raise HTTPException(status_code=403, detail="Solo operadores pueden acceder")
    count = db.query(Ticket).count()
    return {"count": count}

@app.get("/tickets")
def get_tickets(
    status: Optional[str] = None,
    area: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = 0,
    order: Optional[str] = "desc",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    is_jefe = current_user.role == "jefe_cuadrilla"
    if current_user.role not in ["operador", "operator", "supervisor"] and not is_jefe:
        raise HTTPException(status_code=403, detail="Solo operadores o jefes de cuadrilla pueden acceder")

    query = db.query(Ticket)
    if order == "asc":
        query = query.order_by(Ticket.id.asc())
    else:
        query = query.order_by(Ticket.priority_score.desc(), Ticket.id.desc())

    if status:
        query = query.filter(Ticket.status == status)
    if area:
        area_obj = db.query(Area).filter(Area.name == area).first()
        if area_obj:
            query = query.filter(Ticket.area_id == area_obj.id)

    if offset:
        query = query.offset(offset)
    if limit:
        query = query.limit(limit)

    tickets = query.all()
    return [_serialize_ticket(t, db, include_reporter=True) for t in tickets]

@app.get("/tickets/{ticket_id}")
def get_ticket(
    ticket_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    # Ciudadanos solo ven sus propios tickets
    if current_user.role not in ["operador", "operator", "supervisor"] and ticket.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="No tienes permiso para ver este ticket")

    return _serialize_ticket(ticket, db, include_reporter=True)

@app.patch("/tickets/{ticket_id}")
def update_ticket(
    ticket_id: int,
    data: TicketUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    if data.status is not None:
        ticket.status = data.status
    if data.title is not None:
        ticket.title = data.title
    if data.description is not None:
        ticket.description = data.description

    db.commit()
    db.refresh(ticket)
    return _serialize_ticket(ticket, db, include_reporter=True)

@app.patch("/tickets/{ticket_id}/status")
def update_status(
    ticket_id: int,
    request: UpdateStatusRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    ticket.status = request.status
    db.commit()
    return {"message": "Status updated", "new_status": request.status}

@app.delete("/tickets/{ticket_id}")
def delete_ticket(
    ticket_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    # Solo admins/operadores o el dueño pueden eliminar
    if current_user.role not in ["operador", "operator", "supervisor"] and ticket.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="No tienes permiso para eliminar este ticket")

    db.delete(ticket)
    db.commit()
    return {"message": "Ticket deleted"}

# ─── ASIGNACIÓN DE CUADRILLA ──────────────────────────────────────────────────

@app.api_route("/tickets/{ticket_id}/assign", methods=["POST", "PATCH"])
def assign_squad(
    ticket_id: int,
    request: AssignSquadRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.role not in ["operador", "operator", "supervisor"]:
        raise HTTPException(status_code=403, detail="Solo operadores pueden asignar cuadrillas")

    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    # Resolver horas estimadas: request > ticket > default (24h)
    hours = request.estimated_hours or ticket.estimated_hours or 24

    # Si ya tenía cuadrilla distinta, restar horas a la anterior
    if ticket.squad_name and ticket.squad_name != request.squad_name:
        old_squad = db.query(Squad).filter(Squad.name == ticket.squad_name).first()
        if old_squad and old_squad.pending_tasks and old_squad.pending_tasks > 0:
            old_hours = ticket.estimated_hours or hours
            old_squad.pending_tasks = max(0, old_squad.pending_tasks - old_hours)

    ticket.squad_name = request.squad_name
    # Guardar las horas en el ticket para futuras reasignaciones
    ticket.estimated_hours = hours
    if ticket.status == "Recibido":
        ticket.status = "Asignado"

    # Sumar horas estimadas a la nueva cuadrilla
    new_squad = db.query(Squad).filter(Squad.name == request.squad_name).first()
    if new_squad:
        current_hours = new_squad.pending_tasks if new_squad.pending_tasks is not None else 0
        new_squad.pending_tasks = current_hours + hours

    db.commit()
    db.refresh(ticket)
    return {
        "message": "Cuadrilla asignada",
        "squad_name": ticket.squad_name,
        "status": ticket.status,
        "estimated_hours": hours,
    }

# ─── EVIDENCIA ────────────────────────────────────────────────────────────────

@app.post("/tickets/{ticket_id}/evidence")
def add_evidence(
    ticket_id: int,
    request: AddEvidenceRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    existing_count = db.query(Evidence).filter(Evidence.ticket_id == ticket_id).count()
    if existing_count >= 1:
        raise HTTPException(status_code=400, detail="Este ticket ya tiene una foto asociada")

    evidence = Evidence(
        ticket_id=ticket_id,
        image_url=request.image_url,
        description=request.description,
    )
    db.add(evidence)
    db.commit()
    db.refresh(evidence)
    return {
        "message": "Evidence added",
        "evidence_id": evidence.id,
        "id": evidence.id,
        "ticket_id": evidence.ticket_id,
        "image_url": evidence.image_url,
        "description": evidence.description,
        "created_at": evidence.created_at,
    }

@app.get("/tickets/{ticket_id}/evidence")
def get_evidence(
    ticket_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    evidences = db.query(Evidence).filter(Evidence.ticket_id == ticket_id).all()
    return [
        {
            "id": ev.id,
            "ticket_id": ev.ticket_id,
            "image_url": ev.image_url,
            "description": ev.description,
            "created_at": ev.created_at,
        }
        for ev in evidences
    ]

@app.delete("/evidence/{evidence_id}")
def delete_evidence(
    evidence_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ev = db.query(Evidence).filter(Evidence.id == evidence_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Evidence not found")
    db.delete(ev)
    db.commit()
    return {"message": "Evidence deleted"}

# ─── CUADRILLAS ───────────────────────────────────────────────────────────────

@app.get("/squads")
def get_squads(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    squads = db.query(Squad).all()
    
    # Hemos removido la auto-creación para no alterar la BD existente.

    return [
        {
            "id": s.id,
            "name": s.name,
            "area_name": s.area_name,
            "pending_tasks": s.pending_tasks if s.pending_tasks is not None else 0,
            "squad_type": s.squad_type or "cuadrilla",
        }
        for s in squads
    ]

@app.get("/squads/stats")
def get_squad_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Retorna estadísticas detalladas de cada cuadrilla:
    - Nombre
    - Total de tareas
    - Horas asignadas
    - % de resolución dentro de SLA
    """
    squads = db.query(Squad).all()
    stats = []
    
    for squad in squads:
        # Obtener todos los tickets de esta cuadrilla
        tickets = db.query(Ticket).filter(Ticket.squad_name == squad.name).all()
        
        total_tasks = len(tickets)
        total_hours = sum(t.estimated_hours or 0 for t in tickets)
        
        # Calcular tickets dentro de SLA
        completed_tickets = [t for t in tickets if t.status in ['Resuelto', 'Cerrado']]
        on_time = 0
        late = 0
        
        for ticket in completed_tickets:
            # Obtener el área para conocer el SLA
            area = db.query(Area).filter(Area.id == ticket.area_id).first()
            if area and ticket.created_at:
                # Calcular deadline de SLA
                sla_deadline = ticket.created_at + timedelta(hours=area.sla_hours)
                
                # Usar planned_date como proxy de fecha de resolución
                # (idealmente debería haber un campo resolved_at)
                resolution_date = ticket.planned_date
                
                if resolution_date:
                    if resolution_date <= sla_deadline:
                        on_time += 1
                    else:
                        late += 1
        
        total_completed = len(completed_tickets)
        sla_percentage = (on_time / total_completed * 100) if total_completed > 0 else 0
        
        stats.append({
            "id": squad.id,
            "name": squad.name,
            "area_name": squad.area_name,
            "squad_type": squad.squad_type or "cuadrilla",
            "total_tasks": total_tasks,
            "total_hours": round(total_hours, 1),
            "pending_hours": squad.pending_tasks or 0,
            "completed_tasks": total_completed,
            "completed_on_time": on_time,
            "completed_late": late,
            "sla_percentage": round(sla_percentage, 1)
        })
    
    # Ordenar por nombre
    stats.sort(key=lambda x: x["name"])
    
    return stats

# ─── ÁREAS ────────────────────────────────────────────────────────────────────

@app.get("/areas")
def get_areas(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    areas = db.query(Area).all()
    return [
        {
            "id": a.id,
            "name": a.name,
            "sla_hours": a.sla_hours,
        }
        for a in areas
    ]

# ─── ESTADÍSTICAS ─────────────────────────────────────────────────────────────

@app.get("/stats/dashboard")
def get_dashboard_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    all_tickets = db.query(Ticket).all()
    total = len(all_tickets)

    open_tickets = [t for t in all_tickets if t.status not in ("Resuelto", "Cerrado")]
    resolved = [t for t in all_tickets if t.status in ("Resuelto", "Cerrado")]

    at_risk = sum(
        1 for t in open_tickets if t.urgency_level == "Alta"
    )

    # Tiempo promedio de resolución (horas)
    response_times = []
    for t in resolved:
        if t.created_at and t.planned_date:
            delta = (t.planned_date - t.created_at).total_seconds() / 3600
            response_times.append(delta)
    avg_hours = sum(response_times) / len(response_times) if response_times else 0
    avg_response = f"{int(avg_hours)}h {int((avg_hours % 1) * 60)}m"

    resolved_first_contact_pct = round((len(resolved) / total * 100) if total > 0 else 0)

    return {
        "total_open": len(open_tickets),
        "total_tickets": total,
        "resolved_first_contact_percentage": resolved_first_contact_pct,
        "avg_response_time": avg_response,
        "at_risk_count": at_risk,
        "tickets_at_risk": at_risk,
        "tickets_by_status": {
            status: sum(1 for t in all_tickets if t.status == status)
            for status in ["Recibido", "Asignado", "En Gestión", "Resuelto", "Cerrado"]
        },
    }

@app.get("/stats/areas")
def get_area_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    all_tickets = db.query(Ticket).all()
    result: dict = {}

    for ticket in all_tickets:
        area = db.query(Area).filter(Area.id == ticket.area_id).first()
        area_name = area.name if area else "Sin asignar"
        if area_name not in result:
            result[area_name] = {"total": 0, "open": 0, "resolved": 0, "at_risk": 0}
        result[area_name]["total"] += 1
        if ticket.status in ("Resuelto", "Cerrado"):
            result[area_name]["resolved"] += 1
        else:
            result[area_name]["open"] += 1
            if ticket.urgency_level == "Alta":
                result[area_name]["at_risk"] += 1

    return result

# ─── ENDPOINTS IA para frontend (monitor operador) ────────────────────────────

@app.post("/ai/tickets/classify")
def ai_classify_ticket(
    payload: AITicketPayload,
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in ["operador", "operator", "supervisor"]:
        raise HTTPException(status_code=403, detail="Solo operadores pueden acceder a IA de clasificación")

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

    factors = calculate_priority_factors_with_ai(payload.title, payload.description)
    score = compute_priority_score_from_factors(factors, PRIORITY_WEIGHTS)
    urgency = calculate_urgency(score)
    return {
        "score": score,
        "urgency": urgency,
        "metrics": factors,
        "weights": PRIORITY_WEIGHTS,
    }


# ─── Nuevo endpoint: descripción de tarea + tiempo estimado ──────────────────

class AITaskPayload(BaseModel):
    title: str
    description: str
    area: str
    squad_types: List[str]   # nombres de cuadrillas disponibles en el área
    ticket_id: Optional[int] = None  # ← nuevo: si viene, guarda los resultados en el ticket


@app.post("/ai/tickets/task")
def ai_ticket_task(
    payload: AITaskPayload,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Genera una descripción concisa de la tarea y el tiempo estimado de resolución.
    Recibe el área clasificada y los tipos de cuadrillas disponibles como contexto.
    Si se envía ticket_id, persiste task_summary y estimated_hours en el ticket."""

    if current_user.role not in ["operador", "operator", "supervisor"]:
        raise HTTPException(status_code=403, detail="Solo operadores pueden acceder")

    squad_list = ", ".join(payload.squad_types) if payload.squad_types else "cuadrilla general"

    if not _openai_available():
        words = payload.title.split()
        summary = " ".join(words[:10]) + ("…" if len(words) > 10 else "")
        result = {"task_summary": summary, "estimated_hours": 24}
    else:
        messages = [
            {
                "role": "system",
                "content": (
                    "Eres un asistente municipal. Dado un reporte ciudadano y el área de gestión, "
                    "responde SOLO con JSON válido con dos campos:\n"
                    '{"task_summary": "<descripción de la tarea en máximo 15 palabras>", "estimated_hours": <número entero de horas>}\n'
                    "No incluyas texto adicional fuera del JSON."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Área: {payload.area}\n"
                    f"Cuadrillas disponibles: {squad_list}\n"
                    f"Título del reporte: {payload.title}\n"
                    f"Descripción: {payload.description}\n\n"
                    "Devuelve JSON con task_summary (acción concreta para la cuadrilla) y estimated_hours."
                ),
            },
        ]

        try:
            raw = _openai_chat(messages, max_tokens=100)
            data = json.loads(raw)
            summary = str(data.get("task_summary", payload.title[:60]))
            hours = int(data.get("estimated_hours", 24))
            result = {"task_summary": summary, "estimated_hours": max(1, min(hours, 720))}
        except (HTTPException, json.JSONDecodeError, ValueError):
            words = payload.title.split()
            summary = " ".join(words[:10]) + ("…" if len(words) > 10 else "")
            result = {"task_summary": summary, "estimated_hours": 24}

    # ── Persistir en el ticket si se envió ticket_id ──────────────────────
    if payload.ticket_id:
        ticket = db.query(Ticket).filter(Ticket.id == payload.ticket_id).first()
        if ticket:
            ticket.task_summary = result["task_summary"]
            ticket.estimated_hours = result["estimated_hours"]
            db.commit()

    return result


# ─── SQUAD TYPE MANAGEMENT ────────────────────────────────────────────────────

class SquadTypeUpdate(BaseModel):
    squad_type: str  # "patrulla" | "cuadrilla"

@app.patch("/squads/{squad_id}/type")
def update_squad_type(
    squad_id: int,
    body: SquadTypeUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.role not in ["operador", "operator", "supervisor"]:
        raise HTTPException(status_code=403, detail="Solo operadores pueden modificar cuadrillas")
    squad = db.query(Squad).filter(Squad.id == squad_id).first()
    if not squad:
        raise HTTPException(status_code=404, detail="Cuadrilla no encontrada")
    squad.squad_type = body.squad_type
    db.commit()
    return {"id": squad.id, "name": squad.name, "squad_type": squad.squad_type}


# ─── VIT CHAT (contextualised by role) ────────────────────────────────────────

class VITChatRequest(BaseModel):
    message: str
    history: Optional[List[dict]] = []  # [{role: user|assistant, content: str}]
    squad_name: Optional[str] = None    # for jefe_cuadrilla

@app.post("/vit/chat")
def vit_chat(
    body: VITChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Contextualised VIT chat for operators, patrol squads and regular squads."""

    role = current_user.role  # operador | jefe_cuadrilla

    # ── Build context from DB ─────────────────────────────────────────────────
    tickets_ctx = ""
    squad_ctx = ""

    if role in ["operador", "operator", "supervisor"]:
        # Admin gets a summary of all open tickets
        open_tickets = db.query(Ticket).filter(
            Ticket.status.notin_(["Resuelto", "Cerrado"])
        ).order_by(Ticket.priority_score.desc()).limit(20).all()

        if open_tickets:
            lines = []
            for t in open_tickets:
                area = db.query(Area).filter(Area.id == t.area_id).first()
                lines.append(
                    f"- #{t.id} [{t.urgency_level}] {t.title} | Área: {area.name if area else '?'} "
                    f"| Estado: {t.status} | Score: {t.priority_score}"
                )
            tickets_ctx = "TICKETS ABIERTOS (ordenados por prioridad):\n" + "\n".join(lines)

        system_prompt = (
            "Eres VIT, el asistente de gestión municipal de Vitacura para el equipo administrador. "
            "Ayudas al operador a gestionar tickets, priorizar incidentes, asignar cuadrillas y entender el estado operativo. "
            "Responde siempre en español, de forma concisa y útil.\n\n"
            f"{tickets_ctx}"
        )

    elif role == "jefe_cuadrilla":
        # Determine if this squad is a patrol
        squad_name = body.squad_name or ""
        squad = db.query(Squad).filter(Squad.name == squad_name).first()
        is_patrol = squad and (squad.squad_type or "cuadrilla") == "patrulla"

        # Get tickets for this squad
        squad_tickets = db.query(Ticket).filter(
            Ticket.squad_name == squad_name,
            Ticket.status.notin_(["Resuelto", "Cerrado"])
        ).order_by(Ticket.priority_score.desc()).limit(15).all()

        if squad_tickets:
            lines = []
            for t in squad_tickets:
                lines.append(
                    f"- #{t.id} [{t.urgency_level}] {t.title} | Estado: {t.status} "
                    f"| Score: {t.priority_score} | Horas est.: {t.estimated_hours or '?'}"
                )
            tickets_ctx = f"TICKETS ASIGNADOS A {squad_name}:\n" + "\n".join(lines)
        else:
            tickets_ctx = f"Sin tickets activos asignados a {squad_name}."

        if is_patrol:
            system_prompt = (
                "Eres VIT, el asistente de patrulla municipal de Vitacura. "
                "Apoyas a la patrulla con información de incidentes en su cuadrante, "
                "navegación, priorización de respuesta, protocolos de seguridad y comunicación. "
                "Puedes ayudar con rutas, procedimientos de atención, escalado de incidentes "
                "y coordinación con otras unidades. Responde en español, de forma clara y operativa.\n\n"
                f"Cuadrilla: {squad_name}\n"
                f"Tipo: PATRULLA\n\n"
                f"{tickets_ctx}"
            )
        else:
            system_prompt = (
                "Eres VIT, el asistente operativo municipal de Vitacura. "
                "Apoyas a la cuadrilla con información sobre sus tickets asignados, "
                "procedimientos de trabajo, materiales necesarios, estimaciones de tiempo "
                "y cualquier duda técnica o logística sobre su área de trabajo. "
                "Responde en español, de forma práctica y concreta.\n\n"
                f"Cuadrilla: {squad_name}\n"
                f"Tipo: CUADRILLA DE TRABAJO\n\n"
                f"{tickets_ctx}"
            )
    else:
        system_prompt = (
            "Eres VIT, el asistente de la Municipalidad de Vitacura. "
            "Responde preguntas sobre la municipalidad, trámites y servicios. "
            "Responde en español."
        )

    # ── Call OpenAI (or fallback) ─────────────────────────────────────────────
    if not _openai_available():
        # Simple keyword fallback
        msg_lower = body.message.lower()
        if any(w in msg_lower for w in ["ticket", "solicitud", "pendiente", "incidente"]):
            reply = f"Tengo acceso a la información de tickets. {tickets_ctx[:300] if tickets_ctx else 'No hay tickets activos en este momento.'}"
        else:
            reply = "Soy VIT, tu asistente municipal. ¿En qué puedo ayudarte hoy?"
        return {"reply": reply}

    messages = [{"role": "system", "content": system_prompt}]

    # Add conversation history (last 10 messages)
    for h in (body.history or [])[-10:]:
        if h.get("role") in ["user", "assistant"] and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})

    messages.append({"role": "user", "content": body.message})

    try:
        reply = _openai_chat(messages, max_tokens=400)
        return {"reply": reply}
    except Exception:
        return {"reply": "Lo siento, no puedo responder ahora mismo. Intenta de nuevo en un momento."}
