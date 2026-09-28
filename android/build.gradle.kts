plugins {
    id("com.android.application") version "8.9.1" apply false
    id("org.jetbrains.kotlin.android") version "2.1.20" apply false
    id("org.jetbrains.kotlin.plugin.compose") version "2.1.20" apply false
    id("org.jetbrains.kotlin.plugin.serialization") version "2.1.20" apply false
    // Zrzuty ekranow aplikacji rysowane na JVM, bez telefonu i emulatora
    // (docs/zrzuty/aplikacja). 1.3.5, bo nowsze wymagaja Kotlina 2.3 i AGP 9.
    id("app.cash.paparazzi") version "1.3.5" apply false
}
