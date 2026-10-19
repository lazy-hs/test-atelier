FROM node:22-alpine AS build
WORKDIR /app
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
RUN npm run build

FROM nginx:stable-alpine
COPY web/nginx-main.conf /etc/nginx/nginx.conf
COPY web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
COPY web/nginx-entrypoint.sh /usr/local/bin/test-atelier-nginx
RUN chmod 0755 /usr/local/bin/test-atelier-nginx

# Run the edge as the image's unprivileged nginx account.  The configuration
# listens on 8080 and keeps its PID/temp files under /tmp, so the container
# does not need root or Linux capabilities at runtime.
USER nginx

# Keep the container stream JSON-only. The upstream entrypoint prints shell
# bootstrap messages, which makes structured log collection and validation
# ambiguous; nginx itself already handles foreground mode and signals.
ENTRYPOINT ["/usr/local/bin/test-atelier-nginx"]
CMD ["-g", "daemon off;"]
# Let Nginx drain active connections before the read-only web container exits.
STOPSIGNAL SIGQUIT
