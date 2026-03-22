# Usa una imagen base
FROM python:3.10

LABEL maintainer="cdchushig"

# Set Python environment variables

# Instalar dependencias del sistema
RUN apt-get update && apt-get install -y \
    gettext \
    texlive-latex-base \
    texlive-latex-extra \
    texlive-fonts-recommended \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Establece el directorio de trabajo
WORKDIR /var/www

# Añadir la aplicación al contenedor
ADD . /var/www/

# Actualizar pip e instalar dependencias de Python
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install lxml


# Asignar permisos de ejecución
RUN chmod +x /var/www/app/certificate


# Crear usuario no-root
RUN groupadd -r drsnap && useradd -r -g drsnap drsnap

# Directorio de staticfiles fuera del volumen montado
RUN mkdir -p /app/staticfiles && chown -R drsnap:drsnap /app/staticfiles

# Permisos sobre el código
RUN chown -R drsnap:drsnap /var/www

USER drsnap

# Exponer el puerto de la aplicación
EXPOSE 8000
