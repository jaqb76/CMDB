package pl.hubzso.cmdb.data

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import pl.hubzso.cmdb.MainActivity
import pl.hubzso.cmdb.R
import pl.hubzso.cmdb.ui.chwila
import java.time.Instant
import java.util.concurrent.TimeUnit

/**
 * Powiadomienia o nowych zgloszeniach i nowych wiadomosciach od klientow.
 *
 * Serwer nie ma kanalu push (Firebase), wiec telefon sam pyta o liste
 * zgloszen: w tle co 15 minut (najkrotszy odstep, na jaki pozwala Android),
 * a przy otwartej aplikacji co minute. Porownuje ja z tym, co widzial
 * poprzednio, i powiadamia tylko o roznicach.
 *
 * Powiadamiamy o dwoch rzeczach:
 *  - nowe zgloszenie - numeru nie bylo na poprzedniej liscie,
 *  - nowa wiadomosc od klienta - zgloszenie "czeka" (ostatnie slowo nalezy
 *    do klienta) i jego aktywnosc jest nowsza niz przy poprzednim sprawdzeniu.
 * Odpowiedzi technikow i zmiany statusu nie dzwonia - technik wie, co sam
 * zrobil, a o pracy kolegow nie musi byc budzony.
 *
 * Pierwsze sprawdzenie na danym koncie tylko zapamietuje stan: po instalacji
 * nie chcemy kilkudziesieciu powiadomien o starych sprawach.
 */
object PowiadomieniaHelpdesku {
    const val KANAL = "helpdesk"
    const val EXTRA_ZGLOSZENIE = "pl.hubzso.cmdb.zgloszenie"
    private const val PRACA = "helpdesk-powiadomienia"
    private const val PREFS = "helpdesk_powiadomienia"
    private const val KONTO = "konto"
    private const val ZNACZNIK = "znacznik"
    private const val ZNANE = "znane"
    private const val LIMIT_ZNANYCH = 500

    // Worker i petla w aplikacji moga trafic na siebie - stan czytamy
    // i zapisujemy po jednym.
    private val blokada = Mutex()

    /** Wlacza sprawdzanie w tle. Wolane po zalogowaniu; powtorne wolanie nic nie psuje. */
    fun wlacz(context: Context) {
        utworzKanal(context)
        val praca = PeriodicWorkRequestBuilder<SprawdzanieHelpdesku>(15, TimeUnit.MINUTES)
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(PRACA, ExistingPeriodicWorkPolicy.KEEP, praca)
    }

    /** Wylogowanie: koniec sprawdzania i zapomniany stan konta. */
    fun wylacz(context: Context) {
        WorkManager.getInstance(context).cancelUniqueWork(PRACA)
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().clear().apply()
        NotificationManagerCompat.from(context).cancelAll()
    }

    /**
     * Jedno sprawdzenie. Zwraca liczbe wyslanych powiadomien albo null, gdy
     * nie dalo sie sprawdzic (brak sesji, brak helpdesku, blad sieci).
     */
    suspend fun sprawdz(context: Context): Int? = blokada.withLock {
        val session = SessionStore(context.applicationContext)
        session.restore()
        val serwer = session.serverUrl
        if (serwer.isNullOrBlank() || session.token.isNullOrBlank()) return@withLock null
        val fabryka = ApiFactory(session).apply { tenantSlug = session.tenantSlug }
        // Wygasly token (np. godzinny token przy logowaniu palcem) konczy sie
        // tu cichym null - poprosimy o palec dopiero przy otwarciu aplikacji.
        val lista = runCatching {
            val api = fabryka.create(serwer)
            if (!api.helpdeskCatalog().available) return@withLock null
            api.tickets(scope = "all").items
        }.getOrNull() ?: return@withLock null

        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val konto = "${serwer}|${session.loginEmail.orEmpty()}"
        val pierwszyRaz = prefs.getString(KONTO, null) != konto
        val znacznik = if (pierwszyRaz) null else chwila(prefs.getString(ZNACZNIK, null))
        val znane = if (pierwszyRaz) emptySet() else prefs.getStringSet(ZNANE, emptySet()).orEmpty()

        val nowosci = if (pierwszyRaz) emptyList() else wybierzNowosci(lista, znacznik, znane)
        nowosci.forEach { pokaz(context, it) }

        val najnowszy = (lista.mapNotNull { chwila(it.lastActivity) } + listOfNotNull(znacznik)).maxOrNull()
        // Zbior znanych rosnie o numery z listy; przycinamy go, bo lista
        // przychodzi posortowana od najswiezszych, a stare numery nie wroca.
        val noweZnane = (lista.map { it.id } + znane).distinct().take(LIMIT_ZNANYCH).toSet()
        prefs.edit()
            .putString(KONTO, konto)
            .putString(ZNACZNIK, najnowszy?.toString())
            .putStringSet(ZNANE, noweZnane)
            .apply()
        nowosci.size
    }

    private fun pokaz(context: Context, nowosc: Nowosc) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) return
        val zgloszenie = nowosc.ticket
        val otworz = Intent(context, MainActivity::class.java)
            .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            .putExtra(EXTRA_ZGLOSZENIE, zgloszenie.id)
        val intencja = PendingIntent.getActivity(
            context, zgloszenie.id.hashCode(), otworz,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val tytul = if (nowosc.nowe) "Nowe zgłoszenie ${zgloszenie.number}" else "Nowa wiadomość · ${zgloszenie.number}"
        val od = zgloszenie.requesterName?.takeIf { it.isNotBlank() } ?: zgloszenie.requesterEmail
        val tresc = listOf(zgloszenie.subject, od + (zgloszenie.tenant.takeIf { it.isNotBlank() }?.let { " · $it" } ?: ""))
            .joinToString("\n")
        val powiadomienie = NotificationCompat.Builder(context, KANAL)
            .setSmallIcon(R.drawable.ic_powiadomienie)
            .setContentTitle(tytul)
            .setContentText(zgloszenie.subject)
            .setStyle(NotificationCompat.BigTextStyle().bigText(tresc))
            .setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .setContentIntent(intencja)
            .build()
        runCatching { NotificationManagerCompat.from(context).notify(zgloszenie.id.hashCode(), powiadomienie) }
    }

    private fun utworzKanal(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val kanal = NotificationChannel(KANAL, "Helpdesk", NotificationManager.IMPORTANCE_HIGH).apply {
            description = "Nowe zgłoszenia i wiadomości od klientów"
        }
        context.getSystemService(NotificationManager::class.java).createNotificationChannel(kanal)
    }
}

internal data class Nowosc(val ticket: Ticket, val nowe: Boolean)

/** Co z listy zgloszen jest nowe od poprzedniego sprawdzenia. Czysta funkcja - do testow. */
internal fun wybierzNowosci(lista: List<Ticket>, znacznik: Instant?, znane: Set<String>): List<Nowosc> =
    lista.mapNotNull { zgloszenie ->
        val kiedy = chwila(zgloszenie.lastActivity) ?: chwila(zgloszenie.createdAt)
        val swieze = znacznik == null || (kiedy != null && kiedy.isAfter(znacznik))
        when {
            !swieze -> null
            zgloszenie.id !in znane -> Nowosc(zgloszenie, nowe = true)
            zgloszenie.waiting -> Nowosc(zgloszenie, nowe = false)
            else -> null
        }
    }

class SprawdzanieHelpdesku(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        PowiadomieniaHelpdesku.sprawdz(applicationContext)
        // Nieudane sprawdzenie nie jest bledem pracy - nastepne za 15 minut.
        return Result.success()
    }
}
