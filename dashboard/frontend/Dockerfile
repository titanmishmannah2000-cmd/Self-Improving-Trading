FROM node:20-slim AS build
WORKDIR /app
COPY frontend/package.json ./
RUN npm install
COPY frontend/ .
ARG VITE_API_BASE
ENV VITE_API_BASE=${VITE_API_BASE}
RUN npm run build

FROM node:20-slim
WORKDIR /app
COPY --from=build /app/dist ./dist
COPY --from=build /app/server.js ./server.js
EXPOSE 4173
CMD ["node", "server.js"]