# syntax=docker/dockerfile:1

# Local development image. Production build/serve lands in M11.
FROM node:22-alpine AS base

ENV PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH
RUN corepack enable

WORKDIR /app


FROM base AS dev

# pnpm-workspace.yaml carries the `allowBuilds` setting. Without it in the build
# context, pnpm refuses to run esbuild's install script and fails the build with
# ERR_PNPM_IGNORED_BUILDS — so it must be copied alongside the manifest, not with
# the rest of the source below.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
# No BuildKit cache mount: Railway's builder requires the mount id to carry a
# platform-specific `s/<service id>-` prefix, and hardcoding one deployment's
# service id into a shared Dockerfile is worse than the rebuild it saves.
RUN pnpm install --frozen-lockfile

COPY . .

EXPOSE 5173
CMD ["pnpm", "dev"]


# --- deployment -------------------------------------------------------------
#
# Two stages, because the thing that gets served is not the thing that gets
# built: `pnpm build` needs the whole toolchain, and the result is a directory
# of static files that wants a web server, not Node.
#
# **`VITE_API_BASE_URL` is a build argument, not a runtime variable.** Vite
# inlines `import.meta.env.*` into the bundle at compile time — by the time a
# container starts there is no variable left to set, only a string already
# baked into the JavaScript. So the API's public URL has to be known before
# this image is built, which is why the API is deployed first and its domain
# fed in here. Getting this wrong produces a site that loads and then fails
# every request against `http://localhost:8000`, which looks like a CORS bug
# and is not one.
FROM base AS build

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
# No BuildKit cache mount: Railway's builder requires the mount id to carry a
# platform-specific `s/<service id>-` prefix, and hardcoding one deployment's
# service id into a shared Dockerfile is worse than the rebuild it saves.
RUN pnpm install --frozen-lockfile

COPY . .

ARG VITE_API_BASE_URL
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN pnpm build


FROM nginx:1.27-alpine AS prod

# `default.conf.template` is rendered by the image's own envsubst entrypoint,
# which substitutes only names that exist in the environment — so `${PORT}`
# is replaced and nginx's own `$uri` is left alone.
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.default.conf.template /etc/nginx/templates/default.conf.template

ENV PORT=80
EXPOSE 80
