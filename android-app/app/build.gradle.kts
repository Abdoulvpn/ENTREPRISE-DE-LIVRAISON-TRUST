plugins {
    id("com.android.application")
}

val configuredUrl = providers.gradleProperty("TRUSTDELIVERY_URL").orElse("").get()
val escapedUrl = configuredUrl.replace("\\", "\\\\").replace("\"", "\\\"")
fun escapedProperty(name: String): String = providers.gradleProperty(name).orElse("").get()
    .replace("\\", "\\\\").replace("\"", "\\\"")

android {
    namespace = "com.trustdelivery.mobile"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.trustdelivery.mobile"
        minSdk = 24
        targetSdk = 35
        versionCode = 2
        versionName = "1.1.0"
        buildConfigField("String", "WEB_APP_URL", "\"$escapedUrl\"")
        buildConfigField("String", "FIREBASE_APPLICATION_ID", "\"${escapedProperty("FIREBASE_APPLICATION_ID")}\"")
        buildConfigField("String", "FIREBASE_API_KEY", "\"${escapedProperty("FIREBASE_API_KEY")}\"")
        buildConfigField("String", "FIREBASE_PROJECT_ID", "\"${escapedProperty("FIREBASE_PROJECT_ID")}\"")
        buildConfigField("String", "FIREBASE_SENDER_ID", "\"${escapedProperty("FIREBASE_SENDER_ID")}\"")
    }

    buildFeatures {
        buildConfig = true
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation("androidx.activity:activity:1.10.0")
    implementation("androidx.core:core:1.15.0")
    implementation("androidx.swiperefreshlayout:swiperefreshlayout:1.1.0")
    implementation("com.google.firebase:firebase-messaging:24.1.0")
}
