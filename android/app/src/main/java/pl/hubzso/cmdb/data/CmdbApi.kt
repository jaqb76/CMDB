package pl.hubzso.cmdb.data

import com.jakewharton.retrofit2.converter.kotlinx.serialization.asConverterFactory
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.RequestBody
import okhttp3.ResponseBody
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.HttpException
import retrofit2.Retrofit
import retrofit2.http.Body
import retrofit2.http.DELETE
import retrofit2.http.GET
import retrofit2.http.Multipart
import retrofit2.http.POST
import retrofit2.http.PUT
import retrofit2.http.Part
import retrofit2.http.Path
import retrofit2.http.Query
import retrofit2.http.Streaming
import java.util.concurrent.TimeUnit

interface CmdbApi {
    @POST("api/v1/mobile/auth/login") suspend fun login(@Body body: LoginRequest): LoginResponse
    @GET("api/v1/mobile/tenants") suspend fun tenants(): List<Tenant>
    @GET("api/v1/mobile/me") suspend fun me(): User
    @GET("api/v1/mobile/dashboard") suspend fun dashboard(): Dashboard

    @GET("api/v1/mobile/assets")
    suspend fun assets(
        @Query("q") query: String = "",
        @Query("page") page: Int = 1,
        @Query("page_size") pageSize: Int = 50,
        @Query("os_family") osFamily: String = "",
        @Query("unassigned") unassigned: Boolean = false,
    ): AssetPage

    @GET("api/v1/mobile/assets/{id}") suspend fun asset(@Path("id") id: String): AssetDetail

    @PUT("api/v1/mobile/assets/{id}/assignment")
    suspend fun updateAssignment(@Path("id") id: String, @Body body: AssignmentWrite): AssetSummary

    @GET("api/v1/mobile/dictionaries")
    suspend fun dictionaryCategories(): List<DictionaryCategory>

    @GET("api/v1/mobile/dictionaries/{category}/schema")
    suspend fun dictionarySchema(@Path("category") category: String): DictionarySchema

    @GET("api/v1/mobile/dictionaries/{category}")
    suspend fun dictionary(@Path("category") category: String): List<DictionaryEntry>

    @POST("api/v1/mobile/dictionaries/{category}")
    suspend fun createDictionaryEntry(
        @Path("category") category: String,
        @Body body: DictionaryWrite,
    ): DictionaryEntry

    @PUT("api/v1/mobile/dictionaries/{category}/{id}")
    suspend fun updateDictionaryEntry(
        @Path("category") category: String,
        @Path("id") id: String,
        @Body body: DictionaryWrite,
    ): DictionaryEntry

    @DELETE("api/v1/mobile/dictionaries/{category}/{id}")
    suspend fun deleteDictionaryEntry(@Path("category") category: String, @Path("id") id: String)

    @GET("api/v1/mobile/changes") suspend fun changes(@Query("page") page: Int = 1): List<ChangeEntry>
    @GET("api/v1/mobile/reports") suspend fun reports(): List<ReportDefinition>
    @GET("api/v1/mobile/reports/catalog") suspend fun reportCatalog(): ReportCatalog
    @POST("api/v1/mobile/reports") suspend fun createReport(@Body body: ReportWrite): ReportDefinition
    @PUT("api/v1/mobile/reports/{id}") suspend fun updateReport(@Path("id") id: String, @Body body: ReportWrite): ReportDefinition
    @POST("api/v1/mobile/reports/{id}/send") suspend fun sendReport(@Path("id") id: String): ApiMessage
    @DELETE("api/v1/mobile/reports/{id}") suspend fun deleteReport(@Path("id") id: String)

    // --- helpdesk ---
    // Pola formularza jada jednym kawalkiem JSON ("dane"), a pliki osobnymi.
    // Rozpisanie kazdego pola na wlasny kawalek formularza rozjezdzaloby sie
    // z modelem przy pierwszej zmianie po stronie serwera.

    @GET("api/v1/mobile/helpdesk/catalog") suspend fun helpdeskCatalog(): HelpdeskCatalog

    @GET("api/v1/mobile/helpdesk/tickets")
    suspend fun tickets(
        @Query("q") query: String = "",
        @Query("scope") scope: String = "open",
        @Query("page") page: Int = 1,
    ): TicketPage

    @GET("api/v1/mobile/helpdesk/tickets/{id}")
    suspend fun ticket(@Path("id") id: String): TicketDetail

    @GET("api/v1/mobile/helpdesk/assets")
    suspend fun helpdeskAssets(
        @Query("tenant") tenant: String = "",
        @Query("q") query: String = "",
    ): List<TicketAsset>

    @Multipart
    @POST("api/v1/mobile/helpdesk/tickets")
    suspend fun createTicket(
        @Part("dane") dane: RequestBody,
        @Part files: List<MultipartBody.Part>,
    ): TicketSaved

    @Multipart
    @POST("api/v1/mobile/helpdesk/tickets/{id}/messages")
    suspend fun addTicketMessage(
        @Path("id") id: String,
        @Part("dane") dane: RequestBody,
        @Part files: List<MultipartBody.Part>,
    ): TicketSaved

    @POST("api/v1/mobile/helpdesk/tickets/{id}/status")
    suspend fun setTicketStatus(@Path("id") id: String, @Body body: TicketStatusWrite): TicketSaved

    @POST("api/v1/mobile/helpdesk/tickets/{id}/technician")
    suspend fun setTicketTechnician(@Path("id") id: String, @Body body: TicketTechnicianWrite): TicketSaved

    @POST("api/v1/mobile/helpdesk/tickets/{id}/time")
    suspend fun addTicketTime(@Path("id") id: String, @Body body: TicketTimeWrite): TicketSaved

    @POST("api/v1/mobile/helpdesk/tickets/{id}/assets")
    suspend fun changeTicketAsset(@Path("id") id: String, @Body body: TicketAssetWrite): TicketSaved

    @Streaming
    @GET("api/v1/mobile/helpdesk/attachments/{id}")
    suspend fun attachment(@Path("id") id: String): ResponseBody
}

class ApiFactory(private val session: SessionStore) {
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }

    var tenantSlug: String? = null

    fun create(baseUrl: String): CmdbApi {
        val normalized = baseUrl.trim().trimEnd('/') + "/"
        require(normalized.startsWith("https://")) { "Serwer musi używać HTTPS" }
        val logger = HttpLoggingInterceptor().apply { level = HttpLoggingInterceptor.Level.BASIC }
        val client = OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .addInterceptor { chain ->
                val token = session.token
                val request = chain.request().newBuilder().apply {
                    header("Accept", "application/json")
                    tenantSlug?.let { header("X-CMDB-Tenant", it) }
                    if (!token.isNullOrBlank()) header("Authorization", "Bearer $token")
                }.build()
                chain.proceed(request)
            }
            .addInterceptor(logger)
            .build()

        return Retrofit.Builder()
            .baseUrl(normalized)
            .client(client)
            .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
            .build()
            .create(CmdbApi::class.java)
    }
}

fun Throwable.userMessage(): String = when (this) {
    is HttpException -> when (code()) {
        401 -> "Sesja wygasła albo dane logowania są nieprawidłowe."
        403 -> "To konto nie ma uprawnień do tej operacji."
        404 -> "Nie znaleziono danych."
        429 -> {
            val seconds = response()?.headers()?.get("Retry-After")?.toLongOrNull()
            if (seconds == null) "Zbyt wiele prób logowania. Spróbuj ponownie później."
            else {
                val minutes = kotlin.math.ceil(seconds / 60.0).toLong()
                val wait = when {
                    minutes >= 120 -> "około ${kotlin.math.ceil(minutes / 60.0).toLong()} godz."
                    minutes >= 60 -> "około 1 godz."
                    minutes > 1 -> "$minutes min"
                    else -> "1 min"
                }
                "Logowanie jest czasowo zablokowane. Spróbuj ponownie za $wait lub poproś administratora o odblokowanie konta."
            }
        }
        400, 422 -> runCatching {
            val body = response()?.errorBody()?.string().orEmpty()
            val detail = Json.parseToJsonElement(body).let { it as? kotlinx.serialization.json.JsonObject }?.get("detail")
            when (detail) {
                is kotlinx.serialization.json.JsonObject -> detail.entries.joinToString("\n") { "${it.key}: ${it.value}" }
                is kotlinx.serialization.json.JsonPrimitive -> detail.content
                else -> "Sprawdź wymagane pola i format danych."
            }
        }.getOrDefault("Sprawdź wymagane pola i format danych.")
        else -> "Serwer zwrócił błąd ${code()}."
    }
    is java.net.UnknownHostException -> "Nie można odnaleźć serwera."
    is java.net.SocketTimeoutException -> "Serwer nie odpowiedział na czas."
    else -> message ?: "Wystąpił nieoczekiwany błąd."
}
