package pl.hubzso.cmdb

import android.content.Intent
import android.os.Bundle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.fragment.app.FragmentActivity
import pl.hubzso.cmdb.ui.CmdbApp
import pl.hubzso.cmdb.ui.CmdbViewModel

/**
 * FragmentActivity, a nie ComponentActivity: systemowe okno biometrii
 * (androidx.biometric) dziala na fragmentach.
 */
class MainActivity : FragmentActivity() {
    private val vm: CmdbViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent { CmdbApp(vm) }
        if (savedInstanceState == null) obsluzPowrot(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        obsluzPowrot(intent)
    }

    override fun onStart() {
        super.onStart()
        vm.cameForeground()
    }

    override fun onStop() {
        super.onStop()
        // Zmiana konfiguracji (obrot ekranu) to nie wyjscie z aplikacji.
        if (!isChangingConfigurations) vm.wentBackground()
    }

    /** Powrot z przegladarki po logowaniu kontem zewnetrznym. */
    private fun obsluzPowrot(intent: Intent?) {
        val adres = intent?.data ?: return
        if (adres.scheme == "pl.hubzso.cmdb" && adres.path == "/logowanie") {
            adres.getQueryParameter("kod")?.let { vm.finishExternal(it) }
        }
    }
}
