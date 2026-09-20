# Build the ingest service. Multi-stage so the deployed image carries no Go
# toolchain: a ~10 MB static binary on distroless instead of a ~900 MB builder.
FROM golang:1.26-alpine AS build

WORKDIR /src

# Copy manifests first so dependency download is cached independently of
# source edits.
COPY go.mod go.sum ./
RUN go mod download

COPY . .

# CGO is disabled so the binary is fully static and runs on distroless.
# The migrations are embedded into the binary, so the image needs no data files.
RUN CGO_ENABLED=0 GOOS=linux go build \
        -ldflags="-s -w" \
        -o /out/ingestor .

# Distroless, non-root: no shell, no package manager, minimal attack surface.
FROM gcr.io/distroless/static-debian12:nonroot

COPY --from=build /out/ingestor /ingestor

# Railway injects PORT; this is the documented default for local runs.
ENV PORT=8080
EXPOSE 8080

USER nonroot:nonroot
ENTRYPOINT ["/ingestor"]
