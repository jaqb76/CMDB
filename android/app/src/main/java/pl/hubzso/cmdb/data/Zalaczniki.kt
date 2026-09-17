package pl.hubzso.cmdb.data

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.OpenableColumns
import androidx.core.content.FileProvider
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.MultipartBody
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.File

/** Plik wskazany przez technika: z galerii, z dysku albo prosto z aparatu. */
data class WybranyPlik(
    val uri: Uri,
    val nazwa: String,
    val rozmiar: Long,
    val typ: String?,
)

/**
 * Nazwa, ktora wolno zapisac na dysku telefonu.
 *
 * Nazwa zalacznika pochodzi od klienta - przyszla mailem i serwer oddaje ja
 * bez zmian, bo jest opisem, a nie sciezka. Zanim trafi do katalogu podreczego
 * aplikacji, zostawiamy z niej tylko znaki, ktore nie moga wyprowadzic zapisu
 * poza ten katalog.
 */
internal fun bezpiecznaNazwa(nazwa: String): String {
    val oczyszczona = nazwa.map { znak ->
        if (znak.isLetterOrDigit() || znak in "._- ") znak else '_'
    }.joinToString("").trim().trimStart('.')
    return oczyszczona.ifBlank { "zalacznik" }.take(120)
}

/** "245 KB" - rozmiar pliku w postaci, ktora cos znaczy dla czlowieka. */
fun opisRozmiaru(bajty: Long): String = when {
    bajty <= 0 -> ""
    bajty < 1024 -> "$bajty B"
    bajty < 1024 * 1024 -> "${bajty / 1024} KB"
    else -> String.format("%.1f MB", bajty / (1024.0 * 1024.0))
}

/** Nazwa i rozmiar pliku spod adresu content:// - do pokazania przed wysylka. */
fun Context.opisPliku(uri: Uri): WybranyPlik {
    var nazwa = uri.lastPathSegment.orEmpty().substringAfterLast('/')
    var rozmiar = 0L
    runCatching {
        contentResolver.query(uri, null, null, null, null)?.use { kursor ->
            val kolumnaNazwy = kursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            val kolumnaRozmiaru = kursor.getColumnIndex(OpenableColumns.SIZE)
            if (kursor.moveToFirst()) {
                if (kolumnaNazwy >= 0 && !kursor.isNull(kolumnaNazwy)) nazwa = kursor.getString(kolumnaNazwy)
                if (kolumnaRozmiaru >= 0 && !kursor.isNull(kolumnaRozmiaru)) rozmiar = kursor.getLong(kolumnaRozmiaru)
            }
        }
    }
    return WybranyPlik(uri, bezpiecznaNazwa(nazwa), rozmiar, contentResolver.getType(uri))
}

/**
 * Tresc pliku jako kawalek multipartu.
 *
 * Czytamy plik w calosci do pamieci, bo zalaczniki helpdesku maja gorna
 * granice po stronie serwera (kilkanascie megabajtow) i strumieniowanie
 * kosztowaloby wiecej kodu, niz jest warte.
 */
fun Context.czescPliku(plik: WybranyPlik): MultipartBody.Part? {
    val dane = runCatching {
        contentResolver.openInputStream(plik.uri)?.use { it.readBytes() }
    }.getOrNull() ?: return null
    val typ = (plik.typ ?: "application/octet-stream").toMediaTypeOrNull()
    return MultipartBody.Part.createFormData("pliki", plik.nazwa, dane.toRequestBody(typ))
}

/** Katalog na zdjecia z aparatu i pobrane zalaczniki - czyszczony przez system. */
private fun Context.katalog(nazwa: String): File =
    File(cacheDir, nazwa).apply { mkdirs() }

/** Adres pliku, ktory aparat ma zapisac. Pusty plik powstaje od razu. */
fun Context.adresNaZdjecie(): Pair<Uri, File> {
    val plik = File(katalog("zdjecia"), "zdjecie-${System.currentTimeMillis()}.jpg")
    plik.createNewFile()
    return FileProvider.getUriForFile(this, "$packageName.pliki", plik) to plik
}

/** Zapisuje pobrany zalacznik w katalogu podrecznym i oddaje jego adres. */
fun Context.zapiszZalacznik(identyfikator: String, nazwa: String, dane: ByteArray): Uri {
    val plik = File(katalog("zalaczniki"), "$identyfikator-${bezpiecznaNazwa(nazwa)}")
    plik.writeBytes(dane)
    return FileProvider.getUriForFile(this, "$packageName.pliki", plik)
}

/**
 * Otwiera plik w aplikacji, ktora go obsluguje.
 *
 * Zwraca false, gdy telefon nie ma czym otworzyc tego typu - wtedy ekran
 * mowi o tym wprost, zamiast wygladac na zepsuty.
 */
fun Context.otworzPlik(uri: Uri, typ: String?): Boolean {
    val zamiar = Intent(Intent.ACTION_VIEW)
        .setDataAndType(uri, typ?.substringBefore(';')?.trim().orEmpty().ifBlank { "*/*" })
        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
    val wybor = Intent.createChooser(zamiar, "Otwórz załącznik")
        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
    return try {
        startActivity(wybor)
        true
    } catch (brak: ActivityNotFoundException) {
        false
    }
}
