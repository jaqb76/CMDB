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

    suspend fun restore() {
        val values = context.sessionDataStore.data.first()
        token = encrypted.getString(TOKEN, null)
        serverUrl = values[SERVER]
        loginEmail = values[EMAIL]
        tenantSlug = values[TENANT]
    }

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
        val SERVER = stringPreferencesKey("server_url")
        val EMAIL = stringPreferencesKey("login_email")
        val TENANT = stringPreferencesKey("tenant_slug")
    }
}
