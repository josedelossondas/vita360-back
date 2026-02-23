# Vita360 — Setup y Cambios

## Resumen de cambios

### Backend (`main.py`)

**Nuevos endpoints:**
- `GET /me` — devuelve datos del usuario autenticado (id, name, email, role)
- `PATCH /tickets/{id}/assign` — asigna un equipo a un ticket (solo operador)

**Endpoints modificados:**
- `POST /login` — ahora también devuelve `role` y `name` además del token
- `GET /my-tickets` — ahora devuelve detalles completos incluyendo evidencias
- `GET /tickets` — ahora devuelve también `reported_by`, `reported_by_email` y evidencias
- `PATCH /tickets/{id}/status` — ahora acepta body JSON `{"status": "..."}` en vez de query param
- `POST /tickets/{id}/evidence` — ahora acepta body JSON con `image_url` (base64 o URL) y `description`

**Nuevos campos en DB:**
- `Ticket.area_name` — guarda el nombre del área para devolver fácilmente sin JOIN
- `Evidence.description` — texto asociado a la foto subida

**Otros cambios:**
- CORS habilitado (allow_origins: `*`, configurar dominio específico en producción)
- Soporte SQLite como fallback para desarrollo local
- Clasificación mejorada con más categorías (Alumbrado Público, Obras Sanitarias)
- Roles aceptados: `ciudadano` y `operador`

---

### Frontend (React + Vite + Tailwind)

**Nuevos archivos:**
- `src/context/AuthContext.tsx` — contexto global de autenticación con login/register/logout
- `src/app/pages/LoginPage.tsx` — página de login/registro con selector de rol
- `src/app/pages/CiudadanoPage.tsx` — portal ciudadano completo
- `src/app/pages/OperadorPage.tsx` — panel operador con gestión de tickets
- `src/app/components/LayoutOperador.tsx` — layout con sidebar para operadores

**Archivos modificados:**
- `src/app/App.tsx` — wrapeado con `AuthProvider`
- `src/app/routes.tsx` — rutas con auth guard y routing por rol
- `src/app/components/Layout.tsx` — soporte para layout ciudadano (header simple con logout)

**Variables de entorno:**
Crear archivo `.env` en la raíz del frontend:
```
VITE_API_URL=http://localhost:8000
```

---

## Flujo de usuarios

### Ciudadano
1. Se registra en `/login` eligiendo rol "Ciudadano"
2. Al login → redirige a `/ciudadano`
3. Puede crear tickets describiendo el problema
4. Ve sus tickets con estado en tiempo real
5. Puede adjuntar fotos con descripción a sus tickets

### Operador
1. Se registra eligiendo rol "Operador"
2. Al login → redirige a `/operador`
3. Ve todos los tickets ordenados por prioridad IA
4. Puede filtrar por estado y área
5. Selecciona un ticket y asigna equipo
6. Avanza el estado: Recibido → Asignado → En Gestión → Resuelto
7. Puede ver las evidencias/fotos subidas por el ciudadano

---

## Setup desarrollo local

### Backend
```bash
pip install fastapi uvicorn sqlalchemy python-jose bcrypt python-multipart
# (opcionalmente: psycopg2-binary para PostgreSQL)

# Variables de entorno opcionales (sin DATABASE_URL usa SQLite)
export DATABASE_URL=postgresql://user:pass@host/db

uvicorn main:app --reload
```

### Frontend
```bash
npm install
cp .env.example .env
# Editar .env con la URL del backend
npm run dev
```

---

## Estados de tickets

| Estado | Quién lo asigna |
|--------|----------------|
| Recibido | Sistema (al crear ticket) |
| Asignado | Operador (al asignar equipo) |
| En Gestión | Operador (manualmente) |
| Resuelto | Operador (al marcar como resuelto, notifica al ciudadano via estado) |
| Cerrado | Operador |
