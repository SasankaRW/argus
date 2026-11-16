plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "dev.argus.companion"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.argus.companion"
        minSdk = 26
        targetSdk = 35
        versionCode = 3
        versionName = "1.3"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")  // a personal app: the debug key is fine
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

// One library: BouncyCastle, for Ed25519 and X25519 (Argus Send), which Android has natively only from API 33 and 31.
// Everything else is Android itself (HttpURLConnection, org.json, SQLite, WebView).
dependencies {
    implementation("org.bouncycastle:bcprov-jdk18on:1.79")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}
