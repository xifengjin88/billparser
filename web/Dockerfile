# The web app. Stages: `dev` (Vite dev server with hot reload, for make dev) and `serve` (the built app behind
# nginx, which also forwards /api to the API service).
FROM node:24-alpine AS deps
WORKDIR /web
RUN corepack enable
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

FROM deps AS dev
COPY . .
ENV API_URL=http://api:8000
EXPOSE 5173
CMD ["pnpm", "dev"]

FROM deps AS build
COPY . .
RUN pnpm build

FROM nginx:1.27-alpine AS serve
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 80
