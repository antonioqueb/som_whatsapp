FROM node:20-alpine
WORKDIR /app
RUN apk add --no-cache git
COPY package.json ./
RUN npm install --omit=dev
COPY src ./src
ENV PORT=3000 SESSIONS_DIR=/data/sessions
VOLUME ["/data/sessions"]
EXPOSE 3000
CMD ["node", "src/index.js"]
