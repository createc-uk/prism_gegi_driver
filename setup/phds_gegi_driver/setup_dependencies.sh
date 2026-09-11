#!/usr/bin/env bash
# Install the native and Python dependencies needed to build and run the
# C++20 Prism/NATS driver and Prism Python bindings.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    cmake \
    git \
    libboost-regex-dev \
    libboost-system-dev \
    libboost-thread-dev \
    libopencv-dev \
    libsodium-dev \
    ninja-build \
    pkg-config \
    python3 \
    python3-dev \
    python3-matplotlib \
    python3-numpy \
    python3-pip \
    python3-scipy \
    python3-yaml

rm -rf /var/lib/apt/lists/*
