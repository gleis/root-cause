# Preserve the PostgreSQL entrypoint; rebuild its privilege-dropping helper.
FROM --platform=$BUILDPLATFORM golang:1.27.3-alpine@sha256:fa3d596898d9f126af7c01d14ee7597bab3f39cccf392ef36b69fe77cd681f18 AS gosu
ARG TARGETOS=linux
ARG TARGETARCH
WORKDIR /src
ADD https://codeload.github.com/tianon/gosu/tar.gz/refs/tags/1.19 /tmp/gosu.tar.gz
RUN echo 'cd9719b775dbfedae53923c9b0dc792b66d42c51e0b36652ed6f747fbadc0164  /tmp/gosu.tar.gz' | sha256sum -c - && tar -xzf /tmp/gosu.tar.gz --strip-components=1
COPY deploy/postgres/gosu.go.mod ./go.mod
COPY deploy/postgres/gosu.go.sum ./go.sum
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -mod=readonly -trimpath -o /gosu .
FROM postgres:16-alpine@sha256:aa6cb33cf4c19c765cefcf48d96176258800870a9a1e930ad126200f9d825139
COPY --from=gosu --chmod=0755 /gosu /usr/local/bin/gosu
