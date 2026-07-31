# 🚀 AWS EC2 Deployment Quick Start (CRM AI Services)

This repository contains all the necessary production configuration files for deploying **crm-ai-services** on an **AWS EC2** instance connected to **AWS RDS (MySQL)** and **AWS Amplify (Frontend)**.

---

## 📁 Deployment Configuration Files Created

| File | Description |
| :--- | :--- |
| [`.env.production.example`](file:///home/bhargav/crm-ai-services/.env.production.example) | Production environment variables template (RDS, Amplify origins, JWT, etc.) |
| [`Dockerfile`](file:///home/bhargav/crm-ai-services/Dockerfile) | Production Docker image configuration |
| [`docker-compose.yml`](file:///home/bhargav/crm-ai-services/docker-compose.yml) | Docker Compose configuration for container deployment |
| [`crm-ai.service`](file:///home/bhargav/crm-ai-services/crm-ai.service) | Systemd process manager configuration (for running python uvicorn natively) |
| [`nginx.conf`](file:///home/bhargav/crm-ai-services/nginx.conf) | Nginx reverse proxy template (handles CORS, SSE streaming & SSL) |

---

## ⚡ Quick Deployment Steps

### 1. Configure RDS & EC2 Security Groups
- In EC2 Security Group, allow inbound **80 (HTTP)**, **443 (HTTPS)**, and **22 (SSH)**.
- In RDS Security Group, allow inbound **3306 (MySQL)** from your EC2 Security Group ID (`sg-xxxxxxxx`).

### 2. Database Integration Details

#### A. AWS RDS MySQL (Existing CRM Database)
* The AI service connects to your live RDS MySQL database (`DB_HOST`, `DB_USER`, `DB_PWD`, `DB_NAME`).
* **Automatic AI Tables Initialization**: On initial startup, `crm-ai-services` automatically executes `CREATE TABLE IF NOT EXISTS` for **2 AI tracking tables**:
  1. `ai_token_usage`: Tracks LLM chat model token consumption & USD cost per user.
  2. `ai_parsing_token_usage`: Tracks document/email/PDF parsing token consumption & USD cost.
* **No disruption**: Existing CRM tables remain untouched and fully accessible by both your main backend and the AI service.

#### B. MongoDB Setup (AI Sessions & Vector Cache)
The AI service uses MongoDB for chat history (`ai_chat_sessions`, `ai_chat_history`) and vector cache (`ai_vector_cache`):
* **Option 1 (Cloud - MongoDB Atlas)**: Set `MONGODB_URI=mongodb+srv://user:pass@cluster.mongodb.net/crm_ai`.
* **Option 2 (Local EC2 Container)**: Set `MONGODB_URI=mongodb://mongo:27017/crm_ai` (MongoDB container is included in `docker-compose.yml`).

#### C. Independent CRM Backend API Connection
* The AI service connects to your live main backend service for tasks (AI task assignment), entity resolution, email reading, and report APIs.
* **Set `CRM_API_BASE` in `.env`**:
  ```ini
  CRM_API_BASE=https://backend-crm.yourdomain.com/api/v1
  # OR if on the same VPC / internal network:
  # CRM_API_BASE=http://172.31.x.x:3001/api/v1
  ```

### 3. Configure Environment Variables
On EC2, create `.env` from the template:
```bash
cp .env.production.example .env
```
Fill in:
- `CRM_API_BASE`: Independent Backend API URL
- `DB_HOST`: AWS RDS Endpoint
- `DB_USER` & `DB_PWD`: RDS Database Credentials
- `MONGODB_URI`: Atlas connection string OR `mongodb://mongo:27017/crm_ai`
- `ALLOWED_ORIGINS`: Your AWS Amplify domain (`https://main.dxxxx.amplifyapp.com`)



### 3. Deploy via Docker (Recommended)
```bash
docker-compose up -d --build
```
*(Or if using Systemd: `sudo cp crm-ai.service /etc/systemd/system/ && sudo systemctl enable --now crm-ai`)*

### 4. Enable HTTPS & Nginx Reverse Proxy
```bash
sudo cp nginx.conf /etc/nginx/sites-available/crm-ai
sudo ln -s /etc/nginx/sites-available/crm-ai /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl restart nginx
sudo certbot --nginx -d api.yourdomain.com
```

### 5. Update AWS Amplify Environment Variables
In AWS Amplify Console -> App Settings -> Environment variables, point your API URL variable (e.g. `NEXT_PUBLIC_API_URL`) to `https://api.yourdomain.com`.
