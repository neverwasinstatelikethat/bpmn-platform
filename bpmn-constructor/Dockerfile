# Сборка React-приложения; адрес backend передаётся в сборку через
# build-arg REACT_APP_API_URL (см. docker-compose.yml).
FROM node:20-alpine AS build
WORKDIR /app

ARG REACT_APP_API_URL=http://localhost:8765
ENV REACT_APP_API_URL=$REACT_APP_API_URL

COPY package.json package-lock.json ./
RUN npm ci --legacy-peer-deps

COPY public ./public
COPY src ./src
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/build /usr/share/nginx/html
EXPOSE 80
