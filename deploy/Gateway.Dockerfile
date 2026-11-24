FROM node:22-bookworm-slim
WORKDIR /app
COPY web/package*.json ./
RUN npm ci
COPY web ./
ARG VITE_SEREIN_COMMIT
RUN npm run build
CMD ["node", "server/gateway.mjs"]
