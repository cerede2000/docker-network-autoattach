# syntax=docker/dockerfile:1

FROM golang:1.21-alpine AS builder

RUN apk add --no-cache git ca-certificates

WORKDIR /build

# Copy with verbose
COPY src/go.mod ./go.mod
COPY src/go.sum* ./

# Debug: show what we have
RUN echo "=== Files after go.mod copy ===" && ls -la

# Download deps
RUN go mod download

# Copy all source
COPY src/ ./

# Debug: show all files
RUN echo "=== All files ===" && ls -la

# Try to build with verbose
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -v -o docker-network-manager . 2>&1 || (echo "=== BUILD FAILED ==="; ls -la; exit 1)

FROM scratch
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/
COPY --from=builder /build/docker-network-manager /docker-network-manager
USER 65534:65534
ENTRYPOINT ["/docker-network-manager"]
