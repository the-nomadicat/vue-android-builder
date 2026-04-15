FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV ANDROID_HOME=/mount/android-sdk
ENV JAVA17_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
ENV PATH="${ANDROID_HOME}/cmdline-tools/latest/bin:${ANDROID_HOME}/platform-tools:${ANDROID_HOME}/build-tools/34.0.0:${JAVA_HOME}/bin:${PATH}"

# Install Java 17 + Java 21 (Capacitor 6+ requires Java 21), Python 3, curl, unzip, git
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-17-jdk-headless \
    openjdk-21-jdk-headless \
    python3 \
    python3-pip \
    curl \
    unzip \
    zip \
    git \
    ca-certificates \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 22 from nodesource
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install @quasar/cli globally
RUN npm install -g @quasar/cli@latest

WORKDIR /app
COPY init.sh server.py build_manager.py ./
RUN chmod +x init.sh

EXPOSE 8080

CMD ["/app/init.sh"]
