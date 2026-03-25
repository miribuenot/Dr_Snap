# Dr. Snap!

Dr. Snap! is an analytical tool that evaluates your Snap! projects in a variety of computational areas to provide feedback on aspects such as abstraction, logical thinking, synchronization, parallelization, flow control, user interactivity and data representation. This analyzer is a helpful tool to evaluate your own projects, or those of your Snap! students.

---

## Requirements

- [Docker](https://www.docker.com/)
- [Docker Compose](https://docs.docker.com/compose/)

---

## Local deployment

### 1. Clone the repository

```console
git clone https://github.com/miribuenot/Dr_Snap.git
cd Dr_Snap
```

### 2. Set up environment variables

Copy the example file and fill in your own values:

```console
cp .env.example .env
```

Edit `.env` and set at minimum:

- `DRSCRATCH_SECRET_KEY` — generate a secure key with:
  ```console
  python3 -c "import secrets; print(secrets.token_urlsafe(50))"
  ```
- `DRSCRATCH_DATABASE_PASSWORD` and `DRSCRATCH_DATABASE_ROOT_PASSWORD` — set secure passwords
- `DRSCRATCH_DEBUG` — set to `False` for production, `True` only for local development

### 3. Set required permissions

Due to Docker volume mounting, some directories need write permissions for the application user:

```console
sudo chmod -R 777 app/certificate/
sudo chmod -R 777 app/migrations/
sudo chmod -R 777 uploads/
sudo chmod -R 777 csvs/
```

### 4. Build and start

```console
docker compose up --build
```

The application will be available at `http://127.0.0.1:8000`.

---

## Production deployment

For production deployment on a server:

1. Clone the repository on the server
2. Copy `.env.example` to `.env` and fill in production values:
   - `DRSCRATCH_DEBUG=False`
   - `ALLOWED_HOSTS=yourdomain.com`
   - Secure values for all passwords and secret key
3. Make sure a reverse proxy (Nginx or Apache) is configured in front of the application on port 8000
4. Run:
   ```console
   docker compose up --build -d
   ```

The application uses Gunicorn as the production WSGI server with 3 workers.

---

## Accessing containers

```console
# Access the Django container
docker exec -it drscratchv3_django bash

# Access the database container
docker exec -it drscratchv3_database mysql -p
```

---

## Activating translations

```console
docker exec -it drscratchv3_django bash
python manage.py makemessages -l es
python manage.py compilemessages
```

Or using the Makefile:

```console
make translate
```

---

## Project structure

```
Dr_Snap/
├── app/                  # Main Django application
│   ├── certificate/      # LaTeX templates for PDF certificates
│   ├── hairball3/        # Analysis engine
│   ├── templates/        # HTML templates
│   └── views.py          # Main views and security logic
├── drScratch/            # Django configuration (settings, urls, wsgi)
├── static/               # Static files (CSS, JS, images)
├── uploads/              # Temporary uploaded project files
├── docker-compose.yml
├── Dockerfile
├── docker-entrypoint.sh
├── .env.example          # Environment variables template
└── requirements.txt
```