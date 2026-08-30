# syntax=docker/dockerfile:1

# ── Stage 1: build the static bundle ────────────────────────────────────────────
FROM node:20-alpine AS builder

WORKDIR /app

# Install dependencies against the lockfile first for reproducible, cached builds.
COPY package.json package-lock.json ./
RUN npm ci

# Build the production bundle. Same-origin API calls (default) are proxied by nginx,
# so no VITE_API_BASE_URL is baked in unless you explicitly pass one at build time.
COPY . .
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
RUN npm run build

# ── Stage 2: serve with nginx ───────────────────────────────────────────────────
FROM nginx:1.27-alpine AS runtime

# SPA + /api reverse-proxy config. The backend host is templated from an env var
# so the same image works in compose (service name) and other environments.
COPY docker/nginx.conf.template /etc/nginx/templates/default.conf.template

ENV BACKEND_HOST=backend \
    BACKEND_PORT=8000

COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1/health >/dev/null 2>&1 || exit 1
