plugins {
    id("com.android.application")
}

val configuredUrl = providers.gradleProperty("TRUSTDELIVERY_URL").orElse("").get()
val escapedUrl = configuredUrl.replace("\\", "\\\\").replace("\"", "\\\"")

android {
    namespace = "com.trustdelivery.mobile"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.trustdelivery.mobile"
        minSdk = 24
        targetSdk = 35
        versionCode = 1
        versionName = "1.0.0"
        buildConfigField("String", "WEB_APP_URL", "\"$escapedUrl\"")
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
}
