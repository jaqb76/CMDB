package pl.hubzso.cmdb.data

import android.content.Context
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import kotlinx.coroutines.flow.first

private val Context.sessionDataStore by preferencesDataStore("cmdb_session")

class SessionStore(private val context: Context) {
    private val encrypted by lazy {
        val masterKey = MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        EncryptedSharedPreferences.create(
            context,
            "cmdb_secure_session",
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    }
    @Volatile var token: String? = null
        private set
    @Volatile var serverUrl: String? = null
        private set
    @Volatile var loginEmail: String? = null
        private set
    @Volatile var tenantSlug: String? = null
        private set

    /** Identyfikator telefonu na serwerze - gdy biometria jest wlaczona. */
    @Volatile var deviceId: String? = null
        private set
    /** Czy PIN telefonu moze zastapic odcisk palca (zasada firmy). */
    @Volatile var allowDeviceCredential: Boolean = false
        private set
    /** Po ilu minutach w tle aplikacja prosi o palec (zasada firmy). */
    @Volatile var lockAfterMinutes: Int = 5
        private set

    val biometricsEnabled: Boolean get() = !deviceId.isNullOrBlank() && Biometria.maKlucz()

    suspend fun restore() {
        val values = context.sessionDataStore.data.first()
        token = encrypted.getString(TOKEN, null)
        serverUrl = values[SERVER]
        loginEmail = values[EMAIL]
        tenantSlug = values[TENANT]
        deviceId = encrypted.getString(DEVICE, null)
        allowDeviceCredential = encrypted.getBoolean(ALLOW_PIN, false)
        lockAfterMinutes = encrypted.getInt(LOCK_MINUTES, 5)
    }

    fun saveDevice(id: String, policy: MobilePolicy) {
        encrypted.edit()
            .putString(DEVICE, id)
            .putBoolean(ALLOW_PIN, policy.allowDeviceCredential)
            .putInt(LOCK_MINUTES, policy.lockAfterMinutes)
            .apply()
        deviceId = id
        allowDeviceCredential = policy.allowDeviceCredential
        lockAfterMinutes = policy.lockAfterMinutes
    }

    fun savePolicy(policy: MobilePolicy) {
        encrypted.edit()
            .putBoolean(ALLOW_PIN, policy.allowDeviceCredential)
            .putInt(LOCK_MINUTES, policy.lockAfterMinutes)
            .apply()
        allowDeviceCredential = policy.allowDeviceCredential
        lockAfterMinutes = policy.lockAfterMinutes
    }

    fun forgetDevice() {
        encrypted.edit().remove(DEVICE).apply()
        deviceId = null
        Biometria.usunKlucz()
    }

    /** Weryfikator PKCE logowania kontem zewnetrznym - do powrotu z przegladarki. */
    fun savePendingVerifier(verifier: String?) {
        encrypted.edit().apply { if (verifier == null) remove(VERIFIER) else putString(VERIFIER, verifier) }.apply()
    }

    fun pendingVerifier(): String? = encrypted.getString(VERIFIER, null)

    suspend fun saveLogin(server: String, email: String) {
        val normalizedServer = server.trim().trimEnd('/')
        val normalizedEmail = email.trim().lowercase()
        context.sessionDataStore.edit {
            it[SERVER] = normalizedServer
            it[EMAIL] = normalizedEmail
        }
        serverUrl = normalizedServer
        loginEmail = normalizedEmail
    }

    suspend fun saveSession(accessToken: String) {
        encrypted.edit().putString(TOKEN, accessToken).apply()
        token = accessToken
    }

    suspend fun saveTenant(slug: String) {
        context.sessionDataStore.edit { it[TENANT] = slug }
        tenantSlug = slug
    }

    suspend fun clear() {
        encrypted.edit().remove(TOKEN).apply()
        token = null
    }

    private companion object {
        const val TOKEN = "access_token"
        const val DEVICE = "device_id"
        const val ALLOW_PIN = "allow_device_credential"
        const val LOCK_MINUTES = "lock_after_minutes"
        const val VERIFIER = "pending_verifier"
        val SERVER = stringPreferencesKey("server_url")
        val EMAIL = stringPreferencesKey("login_email")
        val TENANT = stringPreferencesKey("tenant_slug")
    }
}
