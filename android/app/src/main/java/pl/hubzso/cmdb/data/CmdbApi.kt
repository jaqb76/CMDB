package pl.hubzso.cmdb.data

import com.jakewharton.retrofit2.converter.kotlinx.serialization.asConverterFactory
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.HttpException
import retrofit2.Retrofit
import retrofit2.http.Body
import retrofit2.http.DELETE
import retrofit2.http.GET
import retrofit2.http.POST
import retrofit2.http.PUT
import retrofit2.http.Path
import retrofit2.http.Query
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
