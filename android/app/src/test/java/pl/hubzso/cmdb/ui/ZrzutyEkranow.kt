package pl.hubzso.cmdb.ui

import androidx.activity.OnBackPressedDispatcher
import androidx.activity.OnBackPressedDispatcherOwner
import androidx.activity.compose.LocalActivityResultRegistryOwner
import androidx.activity.compose.LocalOnBackPressedDispatcherOwner
import androidx.activity.result.ActivityResultRegistry
import androidx.activity.result.ActivityResultRegistryOwner
import androidx.activity.result.contract.ActivityResultContract
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.remember
import androidx.core.app.ActivityOptionsCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleRegistry
import app.cash.paparazzi.DeviceConfig
import app.cash.paparazzi.Paparazzi
import kotlinx.serialization.json.Json
import org.junit.Rule
import org.junit.Test
import pl.hubzso.cmdb.data.AssetDetail
import pl.hubzso.cmdb.data.AssetPage
import pl.hubzso.cmdb.data.Dashboard
import pl.hubzso.cmdb.data.HelpdeskCatalog
import pl.hubzso.cmdb.data.TicketPage
import pl.hubzso.cmdb.data.User

/**
 * Zrzuty ekranow aplikacji do dokumentacji (docs/zrzuty/aplikacja).
 *
 * Rysowane na JVM przez Paparazzi - bez telefonu, emulatora i serwera.
 * Dane to prawdziwe odpowiedzi API mobilnego zapisane przez
 * server/tests/zrzuty_ekranu.py (test_dane_dla_aplikacji), wiec ekrany
 * pokazuja to, co zwraca serwer, a nie dane zmyslone tutaj.
 *
 * CI zapisuje obrazy poleceniem recordPaparazziDebug i przenosi je do
 * dokumentacji; zwykly przebieg testow (verify) ich nie porownuje.
 */
class ZrzutyEkranow {
    @get:Rule
    val paparazzi = Paparazzi(
        deviceConfig = DeviceConfig.PIXEL_6,
        theme = "android:Theme.Material.NoActionBar",
    )

    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }

    private inline fun <reified T> dane(nazwa: String): T {
        val tresc = javaClass.getResource("/zrzuty/$nazwa.json")!!.readText()
        return json.decodeFromString(tresc)
    }

    private fun stan(): AppState {
        val maszyny = dane<AssetPage>("assets")
        val zgloszenia = dane<TicketPage>("tickets")
        val uzytkownik = dane<User>("me")
        return AppState(
            restoring = false,
            user = uzytkownik,
            tenants = listOfNotNull(uzytkownik.tenant),
            dashboard = dane<Dashboard>("dashboard"),
            assets = maszyny.items,
            assetTotal = maszyny.total,
            helpdesk = HelpdeskState(
                available = true,
                catalog = dane<HelpdeskCatalog>("helpdesk_catalog"),
                tickets = zgloszenia.items,
                counters = zgloszenia.counters,
                total = zgloszenia.total,
            ),
        )
    }

    private val helpdesk = HelpdeskActions(
        onOpen = {}, onClose = {}, onSearch = { _, _ -> }, onMore = {}, onRefresh = {},
        onCreate = { _, _ -> }, onSend = { _, _, _ -> }, onStatus = { _, _, _ -> },
        onAssign = { _, _ -> }, onTime = { _, _, _ -> }, onAsset = { _, _, _ -> },
        onSearchAssets = { _, _ -> }, onOpenAttachment = {},
    )

    private fun ekranGlowny(stan: AppState, ciemny: Boolean = true, zakladka: Int = 0, zakladkaKarty: Int = 0) {
        paparazzi.snapshot {
            Podglad(ciemny) {
                CompositionLocalProvider(
                    LocalZakladkaStartowa provides zakladka,
                    LocalZakladkaKartyStartowa provides zakladkaKarty,
                ) {
                    ModernMainScreen(
                        stan, {}, {}, {}, if (ciemny) "dark" else "light",
                        {}, { _, _ -> }, {},
                        {}, {}, { _, _, _ -> }, { _, _ -> },
                        { _, _ -> }, { _, _, _ -> }, {}, {},
                        {}, {}, helpdesk,
                    )
                }
            }
        }
    }

    @Test fun logowanie() = paparazzi.snapshot {
        Podglad(ciemny = true) {
            ModernLoginScreen("https://cmdb.przyklad.pl", "technik@przyklad.pl", false, null, { _, _, _ -> }, {})
        }
    }

    @Test fun pulpit() = ekranGlowny(stan())

    @Test fun pulpitJasny() = ekranGlowny(stan(), ciemny = false)

    @Test fun maszyny() = ekranGlowny(stan(), zakladka = 1)

    @Test fun helpdesk() = ekranGlowny(stan(), zakladka = 2)

    @Test fun wiecej() = ekranGlowny(stan(), zakladka = 3)

    @Test fun kartaMaszyny() = ekranGlowny(stan().copy(selectedAsset = dane<AssetDetail>("asset")))

    @Test fun kartaMaszynyPodatnosci() =
        ekranGlowny(stan().copy(selectedAsset = dane<AssetDetail>("asset")), zakladkaKarty = 2)
}

/**
 * Motyw aplikacji i to, czego ekran oczekuje od Activity: rejestr wynikow
 * (prosba o zgode na powiadomienia) i obsluga przycisku wstecz. W Paparazzi
 * Activity nie ma, wiec podajemy atrapy, ktore niczego nie robia.
 */
@Composable
private fun Podglad(ciemny: Boolean, tresc: @Composable () -> Unit) {
    val rejestr = remember {
        object : ActivityResultRegistryOwner {
            override val activityResultRegistry = object : ActivityResultRegistry() {
                override fun <I, O> onLaunch(
                    requestCode: Int,
                    contract: ActivityResultContract<I, O>,
                    input: I,
                    options: ActivityOptionsCompat?,
                ) = Unit
            }
        }
    }
    val wstecz = remember {
        object : OnBackPressedDispatcherOwner {
            private val cykl = LifecycleRegistry.createUnsafe(this).apply {
                currentState = Lifecycle.State.RESUMED
            }
            override val lifecycle: Lifecycle get() = cykl
            override val onBackPressedDispatcher = OnBackPressedDispatcher()
        }
    }
    CompositionLocalProvider(
        LocalActivityResultRegistryOwner provides rejestr,
        LocalOnBackPressedDispatcherOwner provides wstecz,
    ) {
        CmdbVisualTheme(ciemny) { tresc() }
    }
}
