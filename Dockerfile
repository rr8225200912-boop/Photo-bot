FROM eclipse-temurin:17-jdk

ENV DEBIAN_FRONTEND=noninteractive \
    ANDROID_HOME=/opt/android-sdk \
    ANDROID_SDK_ROOT=/opt/android-sdk \
    PATH=/opt/venv/bin:/opt/gradle/bin:/opt/android-sdk/cmdline-tools/latest/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv unzip wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Gradle
RUN wget -q https://services.gradle.org/distributions/gradle-8.7-bin.zip -O /tmp/g.zip \
    && unzip -q /tmp/g.zip -d /opt && mv /opt/gradle-8.7 /opt/gradle && rm /tmp/g.zip

# Android SDK
RUN mkdir -p $ANDROID_HOME/cmdline-tools \
    && wget -q https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip -O /tmp/c.zip \
    && unzip -q /tmp/c.zip -d $ANDROID_HOME/cmdline-tools \
    && mv $ANDROID_HOME/cmdline-tools/cmdline-tools $ANDROID_HOME/cmdline-tools/latest \
    && rm /tmp/c.zip
RUN yes | sdkmanager --licenses > /dev/null; \
    sdkmanager "platform-tools" "platforms;android-34" "build-tools;34.0.0"

# Python
WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /opt/venv && pip install --no-cache-dir -r requirements.txt
COPY . .

# Warm up Gradle so the first real build is faster (failure here is not fatal)
RUN python -c "import builder, pathlib, shutil; w=pathlib.Path('/tmp/warm'); w2=pathlib.Path('/tmp/webw'); w2.mkdir(); (w2/'index.html').write_text('<html></html>'); builder.prepare_project(w,'warm',w2)" \
    && cd /tmp/warm && (gradle assembleDebug --no-daemon --console=plain || true) \
    && rm -rf /tmp/warm /tmp/webw

CMD ["python", "bot.py"]
