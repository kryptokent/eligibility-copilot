# Eligibility Copilot

A browser-based tool for government benefits caseworkers. Upload documents (pay stub, lease, bank statement); the system extracts eligibility-relevant facts and returns a checklist with citations. Staff review, accept or override items, and record determinations. All overrides are logged.

## Project structure

- **backend/** — Python API (FastAPI) that will handle document upload, AWS Textract, and Bedrock.
- **frontend/** — React app (Vite) that caseworkers use in the browser.

## Quick start

### 1. Install backend (Python) packages

```bash
cd eligibility-copilot/backend
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

*(On Mac/Linux use `source venv/bin/activate` instead of `venv\Scripts\activate`.)*

### 2. Install frontend (Node) packages

Open a **second** terminal:

```bash
cd eligibility-copilot/frontend
npm install
```

### 3. Start the backend server

In the first terminal (with the backend venv activated):

```bash
cd eligibility-copilot/backend
venv\Scripts\activate
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Backend will be at **http://localhost:8000**.

### 4. Start the frontend

In the second terminal:

```bash
cd eligibility-copilot/frontend
npm run dev
```

Frontend will be at **http://localhost:5173**.

### 5. Confirm everything works

- Open **http://localhost:5173** in your browser. You should see: a dark navy header with "Eligibility Copilot" and "AI-Assisted Benefits Intake Review", a centered card with dashed border saying "Document upload coming soon — setup complete", and a green "System Ready" badge at the bottom.
- Open **http://localhost:8000/health** — you should see: `{"status":"ok","version":"1.0"}`.
- Open **http://localhost:8000/** — you should see: `{"message":"Eligibility Copilot API"}`.
