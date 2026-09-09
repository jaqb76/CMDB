package pl.hubzso.cmdb.ui

import java.time.Instant
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.time.ZoneOffset
import java.time.format.DateTimeParseException

/**
 * Serwer oddaje znaczniki czasu jako ISO-8601 ze strefa (kolumny sa
 * DateTime(timezone=True)). Telefon pokazuje je wzglednie - "5 min temu" niesie
 * wiecej niz data z sekundami, a przy okazji nie zmusza nas do zgadywania, w
 * ktorej strefie siedzi uzytkownik.
 *
 * minSdk aplikacji to 26, wiec java.time jest dostepne bez desugarowania.
 */
internal fun chwila(iso: String?): Instant? {
    if (iso.isNullOrBlank()) return null
    return try {
        OffsetDateTime.parse(iso).toInstant()
    } catch (blad: DateTimeParseException) {
        // Starsze wpisy moga byc bez strefy. Traktujemy je jako UTC, bo tak je
        // zapisuje serwer - lepsze to niz pokazanie "brak kontaktu".
        try {
            LocalDateTime.parse(iso).toInstant(ZoneOffset.UTC)
        } catch (drugi: DateTimeParseException) {
            null
        }
    }
}

/** Ile minut minelo od podanej chwili. Null oznacza brak danych, nie zero. */
internal fun minutOd(iso: String?, teraz: Instant = Instant.now()): Long? {
    val kiedy = chwila(iso) ?: return null
    val minuty = (teraz.toEpochMilli() - kiedy.toEpochMilli()) / 60_000
    return if (minuty < 0) 0 else minuty
}

/**
 * Opis w stylu "18 min temu". Skroty "min" i "godz." sa nieodmienne, wiec
 * jedyna liczba mnoga do obsluzenia to dzien/dni.
 */
internal fun wzglednyCzas(iso: String?, teraz: Instant = Instant.now()): String {
    val minuty = minutOd(iso, teraz) ?: return "brak kontaktu"
    return when {
        minuty < 1 -> "przed chwilą"
        minuty < 60 -> "$minuty min temu"
        minuty < 24 * 60 -> "${minuty / 60} godz. temu"
        else -> {
            val dni = minuty / (24 * 60)
            if (dni == 1L) "1 dzień temu" else "$dni dni temu"
        }
    }
}
