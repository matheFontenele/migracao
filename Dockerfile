# Usa uma imagem oficial enxuta do Python 3.12
FROM python:3.12-slim

# Define o diretório de trabalho no contêiner
WORKDIR /app

# Variáveis de ambiente de otimização nativa do Python
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Instala dependências do sistema operacional necessárias para pacotes como PyArrow / PyMySQL
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    default-libmysqlclient-dev \
    && rm -rf /var/lib/apt/lists/*

# Copia e instala as bibliotecas antes do código (Estratégia de Cache do Docker)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o restante do código-fonte
COPY . .

# DEFINE O MÓDULO COMO EXECUTÁVEL PADRÃO DA IMAGEM
ENTRYPOINT ["python", "main.py"]

# Argumento padrão caso o usuário digite apenas "docker run <imagem>"
CMD ["--help"]