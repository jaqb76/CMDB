package pl.hubzso.cmdb.data

import android.content.Context
import android.os.Build
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyPermanentlyInvalidatedException
import android.security.keystore.KeyProperties
import android.security.keystore.StrongBoxUnavailableException
import android.util.Base64
import androidx.biometric.BiometricManager
import androidx.biometric.BiometricManager.Authenticators.BIOMETRIC_STRONG
import androidx.biometric.BiometricManager.Authenticators.DEVICE_CREDENTIAL
import androidx.biometric.BiometricPrompt
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity
import kotlinx.coroutines.suspendCancellableCoroutine
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/**
 * Logowanie odciskiem palca.
 *
 * Klucz prywatny powstaje w sprzetowym sejfie telefonu (Android Keystore,
 * StrongBox gdy jest) i nigdy go nie opuszcza. Kazde uzycie wymaga silnej
 * biometrii, a dodanie nowego odcisku w telefonie uniewaznia klucz - ktos,
 * kto dopisal swoj palec, nie zaloguje sie nim na cudze konto.
 *
 * Serwer zna tylko klucz publiczny i sprawdza nim podpis jednorazowego
 * wyzwania. Sam odcisk palca nie trafia nigdzie - ani do aplikacji, ani na
 * serwer.
 */
object Biometria {
    private const val ALIAS = "cmdb_biometria"
    private const val KEYSTORE = "AndroidKeyStore"

    /** Klucz uniewazniony (nowy odcisk w telefonie) - potrzebne pelne logowanie. */
    class KluczUniewazniony : Exception(
        "Dodano nowy odcisk palca w telefonie. Zaloguj się hasłem, żeby ponownie włączyć biometrię."
    )

    class Anulowano(message: String) : Exception(message)

    private fun uprawnienia(pin: Boolean): Int =
        if (pin) BIOMETRIC_STRONG or DEVICE_CREDENTIAL else BIOMETRIC_STRONG

    /**
     * Biometria dziala od Androida 11. Dopiero tam klucz moze wymagac
     * potwierdzenia przy kazdym uzyciu z wyborem rodzaju potwierdzenia,
     * a okno biometrii jest zawsze systemowe - starsze wersje korzystaja
     * z zastepczego okna biblioteki, ktore wymaga innego motywu aplikacji.
     */
    fun dostepna(context: Context, pin: Boolean): Boolean =
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.R &&
            BiometricManager.from(context).canAuthenticate(uprawnienia(pin)) == BiometricManager.BIOMETRIC_SUCCESS

    fun maKlucz(): Boolean = keyStore().containsAlias(ALIAS)

    fun usunKlucz() {
        runCatching { keyStore().deleteEntry(ALIAS) }
    }

    private fun keyStore(): KeyStore = KeyStore.getInstance(KEYSTORE).apply { load(null) }

    /** Tworzy nowa pare kluczy i zwraca klucz publiczny (DER SPKI, base64). */
    @androidx.annotation.RequiresApi(Build.VERSION_CODES.R)
    fun nowyKlucz(pin: Boolean): String {
        usunKlucz()
        fun specyfikacja(strongBox: Boolean): KeyGenParameterSpec {
            val builder = KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_SIGN)
                .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
                .setDigests(KeyProperties.DIGEST_SHA256)
                .setUserAuthenticationRequired(true)
                .setInvalidatedByBiometricEnrollment(true)
            // 0 sekund = potwierdzenie przy KAZDYM uzyciu klucza.
            builder.setUserAuthenticationParameters(
                0,
                if (pin) KeyProperties.AUTH_BIOMETRIC_STRONG or KeyProperties.AUTH_DEVICE_CREDENTIAL
                else KeyProperties.AUTH_BIOMETRIC_STRONG,
            )
            if (strongBox) builder.setIsStrongBoxBacked(true)
            return builder.build()
        }
        val generator = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, KEYSTORE)
        val para = try {
            generator.initialize(specyfikacja(strongBox = true))
            generator.generateKeyPair()
        } catch (e: StrongBoxUnavailableException) {
            // Telefon bez osobnego ukladu StrongBox - klucz w TEE, tez sprzetowy.
            generator.initialize(specyfikacja(strongBox = false))
            generator.generateKeyPair()
        }
        return Base64.encodeToString(para.public.encoded, Base64.NO_WRAP)
    }

    private fun podpisDoPotwierdzenia(): Signature {
        val klucz = keyStore().getKey(ALIAS, null) as? PrivateKey
            ?: throw KluczUniewazniony()
        return try {
            Signature.getInstance("SHA256withECDSA").apply { initSign(klucz) }
        } catch (e: KeyPermanentlyInvalidatedException) {
            usunKlucz()
            throw KluczUniewazniony()
        }
    }

    /**
     * Pokazuje systemowe okno biometrii i podpisuje [tresc] kluczem z sejfu.
     * Zwraca podpis (DER, base64).
     */
    suspend fun podpisz(activity: FragmentActivity, tresc: String, pin: Boolean, tytul: String): String {
        val podpis = podpisDoPotwierdzenia()
        val wynik = suspendCancellableCoroutine { cont ->
            val prompt = BiometricPrompt(
                activity,
                ContextCompat.getMainExecutor(activity),
                object : BiometricPrompt.AuthenticationCallback() {
                    override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                        val s = result.cryptoObject?.signature
                        if (s == null) {
                            cont.resumeWithException(Anulowano("Telefon nie potwierdził klucza."))
                            return
                        }
                        runCatching {
                            s.update(tresc.toByteArray(Charsets.UTF_8))
                            Base64.encodeToString(s.sign(), Base64.NO_WRAP)
                        }.onSuccess { cont.resume(it) }.onFailure { cont.resumeWithException(it) }
                    }

                    override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
                        if (cont.isActive) cont.resumeWithException(Anulowano(errString.toString()))
                    }
                },
            )
            val info = BiometricPrompt.PromptInfo.Builder()
                .setTitle(tytul)
                .setSubtitle("CMDB")
                .setAllowedAuthenticators(uprawnienia(pin))
                .apply { if (uprawnienia(pin) == BIOMETRIC_STRONG) setNegativeButtonText("Anuluj") }
                .build()
            prompt.authenticate(info, BiometricPrompt.CryptoObject(podpis))
            cont.invokeOnCancellation { prompt.cancelAuthentication() }
        }
        return wynik
    }
}
