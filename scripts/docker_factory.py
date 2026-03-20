import os
import json

def generate_docker_files(stack):
    docker_compose = """version: '3.8'
services:
"""
    if "Node.js/NPM" in stack:
        docker_compose += """  app:
    build: .
    ports:
      - "3000:3000"
    volumes:
      - .:/app
    environment:
      - NODE_ENV=development
"""
        dockerfile = """FROM node:18-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
CMD ["npm", "run", "dev"]
"""
    elif "PHP/Composer" in stack:
        docker_compose += """  app:
    build: .
    ports:
      - "8000:80"
    volumes:
      - .:/var/www/html
"""
        dockerfile = """FROM php:8.2-apache
RUN docker-php-ext-install pdo pdo_mysql
WORKDIR /var/www/html
COPY . .
"""
    else:
        # Default simple dockerfile
        docker_compose += """  web:
    image: nginx:alpine
    ports:
      - "80:80"
"""
        dockerfile = "FROM nginx:alpine\n"

    with open("docker-compose.yml", "w") as f:
        f.write(docker_compose)
    with open("Dockerfile", "w") as f:
        f.write(dockerfile)
    with open(".dockerignore", "w") as f:
        f.write("node_modules\n.git\n.github\n")

if __name__ == "__main__":
    # In a real scenario, this would detect stack from the environment or args
    # For now, we'll simulate detection or take from shell
    import sys
    detected_stack = sys.argv[1:] if len(sys.argv) > 1 else []
    generate_docker_files(detected_stack)
    print("Docker Factory: Generated base configuration.")
