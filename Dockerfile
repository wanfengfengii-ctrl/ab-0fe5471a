# syntax=docker/dockerfile:1

# ---- test stage: gofmt + vet + unit tests (fails the build on any issue) ----
FROM golang:1.22-alpine AS test
WORKDIR /src
COPY go.mod ./
COPY *.go ./
RUN test -z "$(gofmt -l .)" && go vet ./... && go test ./...

# ---- build stage: static, stripped binary ----
FROM test AS build
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/server .

# ---- runtime stage ----
FROM alpine:3.20
RUN adduser -D -u 10001 appuser
COPY --from=build /out/server /usr/local/bin/server
ENV APP_PORT=8080
EXPOSE 8080
USER appuser
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD ["/usr/local/bin/server", "-healthcheck"]
ENTRYPOINT ["/usr/local/bin/server"]
