# Vita360 — Guía de Deploy (Render + Vercel)

## ORDEN: primero backend, después frontend

---

## 1. BACKEND en Render

### Opción A — render.yaml (automático)
1. Sube la carpeta `vita360-back-main/` a un repo de GitHub
2. En Render: **New > Blueprint** → conecta el repo → Render lee el `render.yaml` y crea todo solo
3. Esperar que termine el deploy

### Opción B — Manual
1. **New Web Service** → conecta tu repo de GitHub
   - **Environment:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`

2. **New PostgreSQL** → crear base de datos gratuita llamada `vita360-db`

3. En el Web Service, ir a **Environment > Environment Variables** y agregar:

   | Variable | Valor |
   |----------|-------|
   | `DATABASE_URL` | (copiar "Internal Database URL" desde la DB de Render) |
   | `SECRET_KEY` | (una string larga aleatoria, ej: `openssl rand -hex 32`) |
   | `FRONTEND_URL` | `https://TU-PROYECTO.vercel.app` ← completar después del paso 2 |

4. Copiar la URL del servicio: `https://vita360-api.onrender.com` (o similar)

---

## 2. FRONTEND en Vercel

1. Sube la carpeta `vita360_styled/` (el frontend) a un repo de GitHub

2. En Vercel: **New Project** → importar el repo
   - **Framework Preset:** Vite
   - **Root Directory:** `/` (o la raíz del repo)
   - **Build Command:** `npm run build`
   - **Output Directory:** `dist`

3. En **Environment Variables** agregar:

   | Variable | Valor |
   |----------|-------|
   | `VITE_API_URL` | `https://vita360-api.onrender.com` ← URL del paso 1 |

4. Deploy → copiar la URL de Vercel: `https://vita360-xxxxx.vercel.app`

---

## 3. CONECTAR los dos

1. Volver a Render → Web Service → **Environment**
2. Actualizar `FRONTEND_URL` con la URL real de Vercel:
   ```
   FRONTEND_URL = https://vita360-xxxxx.vercel.app
   ```
3. Render hace redeploy automático con el CORS correcto

---

## Verificar que funciona

```bash
# Healthcheck del backend
curl https://vita360-api.onrender.com/health
# Esperado: {"status":"healthy"}

# Test CORS desde el frontend
# Abrir consola del navegador en tu URL de Vercel y ejecutar:
fetch('https://vita360-api.onrender.com/').then(r => r.json()).then(console.log)
# Esperado: {"status":"ok","service":"Vita360 API"}
```

---

## Notas importantes

- **Render free tier** hiberna el servicio después de 15 min sin tráfico → el primer request puede tardar ~30s en despertar. Normal.
- **postgres:// vs postgresql://** — el backend lo corrige automáticamente, no hace falta cambiarlo a mano.
- **Imágenes base64** — las fotos se guardan como base64 en la DB. Para producción seria con muchos usuarios conviene usar un bucket S3/Cloudinary, pero para este proyecto funciona bien.
- **SECRET_KEY** — nunca commitear la clave real al repo. Siempre por variable de entorno.

