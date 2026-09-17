package pl.hubzso.cmdb.ui

import android.app.Application
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.clickable
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.MenuBook
import androidx.compose.material.icons.outlined.Assessment
import androidx.compose.material.icons.outlined.Computer
import androidx.compose.material.icons.outlined.Dashboard
import androidx.compose.material.icons.outlined.History
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import pl.hubzso.cmdb.data.ApiFactory
import pl.hubzso.cmdb.data.AssetDetail
import pl.hubzso.cmdb.data.AssetPage
import pl.hubzso.cmdb.data.AssetSummary
import pl.hubzso.cmdb.data.AssignmentWrite
import pl.hubzso.cmdb.data.ChangeEntry
import pl.hubzso.cmdb.data.CmdbApi
import pl.hubzso.cmdb.data.Dashboard
import pl.hubzso.cmdb.data.DictionaryCategory
import pl.hubzso.cmdb.data.DictionaryEntry
import pl.hubzso.cmdb.data.DictionarySchema
import pl.hubzso.cmdb.data.DictionaryWrite
import pl.hubzso.cmdb.data.HelpdeskCatalog
import pl.hubzso.cmdb.data.LoginRequest
import pl.hubzso.cmdb.data.NewMessage
import pl.hubzso.cmdb.data.NewTicket
import pl.hubzso.cmdb.data.ReportCatalog
import pl.hubzso.cmdb.data.ReportDefinition
import pl.hubzso.cmdb.data.ReportWrite
import pl.hubzso.cmdb.data.SessionStore
import pl.hubzso.cmdb.data.Tenant
import pl.hubzso.cmdb.data.Ticket
import pl.hubzso.cmdb.data.TicketAsset
import pl.hubzso.cmdb.data.TicketAssetWrite
import pl.hubzso.cmdb.data.TicketAttachment
import pl.hubzso.cmdb.data.TicketCounters
import pl.hubzso.cmdb.data.TicketDetail
import pl.hubzso.cmdb.data.TicketSaved
import pl.hubzso.cmdb.data.TicketStatusWrite
import pl.hubzso.cmdb.data.TicketTechnicianWrite
import pl.hubzso.cmdb.data.TicketTimeWrite
import pl.hubzso.cmdb.data.User
import pl.hubzso.cmdb.data.WybranyPlik
import pl.hubzso.cmdb.data.czescPliku
import pl.hubzso.cmdb.data.otworzPlik
import pl.hubzso.cmdb.data.userMessage
import pl.hubzso.cmdb.data.zapiszZalacznik

private enum class Section(val label: String, val icon: ImageVector) {
    DASHBOARD("Pulpit", Icons.Outlined.Dashboard),
    ASSETS("Maszyny", Icons.Outlined.Computer),
    PEOPLE("Słowniki", Icons.AutoMirrored.Outlined.MenuBook),
    CHANGES("Zmiany", Icons.Outlined.History),
    REPORTS("Raporty", Icons.Outlined.Assessment),
}

data class AppState(
    val restoring: Boolean = true,
    val loading: Boolean = false,
    val saving: Boolean = false,
    val mutationVersion: Int = 0,
    val tenants: List<Tenant> = emptyList(),
    val tenantRequired: Boolean = false,
    val assetTotal: Int = 0,
    val assetPage: Int = 1,
    val assetLoading: Boolean = false,
    val assetQuery: String = "",
    val assetOs: String = "",
    val assetUnassigned: Boolean = false,
    val rememberedServer: String = "",
    val rememberedEmail: String = "",
    val user: User? = null,
    val dashboard: Dashboard? = null,
    val assets: List<AssetSummary> = emptyList(),
    val selectedAsset: AssetDetail? = null,
    val people: List<DictionaryEntry> = emptyList(),
    val dictionaryCategories: List<DictionaryCategory> = emptyList(),
    val dictionarySchemas: Map<String, DictionarySchema> = emptyMap(),
    val dictionaries: Map<String, List<DictionaryEntry>> = emptyMap(),
    val changes: List<ChangeEntry> = emptyList(),
    val reports: List<ReportDefinition> = emptyList(),
    val reportCatalog: ReportCatalog? = null,
    val helpdesk: HelpdeskState = HelpdeskState(),
    val error: String? = null,
    // Komunikat z serwera po udanym zapisie ("Odpowiedź poszła do ..."). Nie
    // jest bledem, ale technik musi go zobaczyc - zwlaszcza wtedy, gdy mowi,
    // ze mail NIE wyszedl mimo zapisanej odpowiedzi.
    val notice: String? = null,
)

/**
 * Helpdesk chodzi po firmach, ktore obsluguje konto - nie po firmie wybranej
 * w aplikacji. Dlatego ma wlasny kawalek stanu i wlasne odswiezanie: zmiana
 * firmy w reszcie aplikacji nie zmienia tu niczego.
 */
data class HelpdeskState(
    val available: Boolean = false,
    val catalog: HelpdeskCatalog? = null,
    val tickets: List<Ticket> = emptyList(),
    val counters: TicketCounters = TicketCounters(),
    val total: Int = 0,
    val page: Int = 1,
    val loading: Boolean = false,
    val query: String = "",
    val scope: String = "open",
    val detail: TicketDetail? = null,
    val assetPicker: List<TicketAsset> = emptyList(),
)

private data class RefreshPayload(
    val dashboard: Dashboard,
    val assets: AssetPage,
    val categories: List<DictionaryCategory>,
    val schemas: Map<String, DictionarySchema>,
    val dictionaries: Map<String, List<DictionaryEntry>>,
    val changes: List<ChangeEntry>,
    val reports: List<ReportDefinition>,
    val reportCatalog: ReportCatalog,
)

class CmdbViewModel(application: Application) : AndroidViewModel(application) {
    private val session = SessionStore(application)
    private val factory = ApiFactory(session)
    private var api: CmdbApi? = null
    private val _state = MutableStateFlow(AppState())
    val state: StateFlow<AppState> = _state.asStateFlow()

    private var assetSearch: Job? = null

    private suspend fun establishTenant(user: User) {
        val available = api!!.tenants()
        val selected = user.tenant
            ?: available.firstOrNull { it.slug == session.tenantSlug }
            ?: available.singleOrNull()
        factory.tenantSlug = selected?.slug
        val scopedUser = if (selected != null) api!!.me() else user
        _state.value = AppState(
            restoring = false,
            rememberedServer = session.serverUrl.orEmpty(),
            rememberedEmail = session.loginEmail.orEmpty(),
            user = scopedUser,
            tenants = available,
            tenantRequired = selected == null,
        )
        if (selected != null) {
            session.saveTenant(selected.slug)
            refreshAll()
        }
    }

    fun chooseTenant() {
        assetSearch?.cancel()
        factory.tenantSlug = null
        _state.value = _state.value.copy(
            loading = false,
            tenantRequired = true,
            dashboard = null,
            assets = emptyList(),
            error = null,
        )
    }

    fun selectTenant(tenant: Tenant) = viewModelScope.launch {
        assetSearch?.cancel()
        factory.tenantSlug = tenant.slug
        _state.value = _state.value.copy(loading = true, error = null)
        runCatching {
            session.saveTenant(tenant.slug)
            api!!.me()
        }.onSuccess {
            _state.value = AppState(restoring = false, user = it, tenants = _state.value.tenants)
            refreshAll()
        }.onFailure { _state.value = _state.value.copy(error = it.userMessage()) }
    }

    init {
        viewModelScope.launch {
            session.restore()
            val server = session.serverUrl
            if (!server.isNullOrBlank() && !session.token.isNullOrBlank()) {
                runCatching {
                    api = factory.create(server)
                    establishTenant(api!!.me())
                }.onFailure {
                    session.clear()
                    _state.value = AppState(
                        restoring = false,
                        rememberedServer = session.serverUrl.orEmpty(),
                        rememberedEmail = session.loginEmail.orEmpty(),
                        error = it.userMessage(),
                    )
                }
            } else {
                _state.value = AppState(
                    restoring = false,
                    rememberedServer = session.serverUrl.orEmpty(),
                    rememberedEmail = session.loginEmail.orEmpty(),
                )
            }
        }
    }

    fun login(server: String, email: String, password: String) = viewModelScope.launch {
        _state.value = _state.value.copy(loading = true, error = null)
        runCatching {
            require(server.trim().startsWith("https://")) { "Adres musi rozpoczynać się od https://" }
            session.saveLogin(server, email)
            val temporary = factory.create(session.serverUrl!!)
            val result = temporary.login(LoginRequest(session.loginEmail!!, password))
            session.saveSession(result.accessToken)
            api = factory.create(server)
            establishTenant(result.user)
        }.onFailure {
            _state.value = _state.value.copy(loading = false, error = it.userMessage())
        }
    }

    fun logout() = viewModelScope.launch {
        assetSearch?.cancel()
        session.clear()
        factory.tenantSlug = null
        api = null
        _state.value = AppState(
            restoring = false,
            rememberedServer = session.serverUrl.orEmpty(),
            rememberedEmail = session.loginEmail.orEmpty(),
        )
    }

    fun refreshAll() = viewModelScope.launch {
        val service = api ?: return@launch
        if (factory.tenantSlug.isNullOrBlank()) {
            _state.value = _state.value.copy(
                loading = false,
                tenantRequired = true,
                error = "Wybierz firmę przed pobraniem danych.",
            )
            return@launch
        }
        val userId = _state.value.user?.id
        val tenantSlug = factory.tenantSlug
        val filters = Triple(_state.value.assetQuery, _state.value.assetOs, _state.value.assetUnassigned)
        _state.value = _state.value.copy(loading = true, error = null)
        runCatching {
            val dashboard = service.dashboard()
            val assets = service.assets(query = filters.first, osFamily = filters.second, unassigned = filters.third)
            val categories = service.dictionaryCategories()
            val schemas = categories.associate { it.key to service.dictionarySchema(it.key) }
            val dictionaries = categories.associate { it.key to service.dictionary(it.key) }
            val changes = service.changes()
            val reports = service.reports()
            val reportCatalog = service.reportCatalog()
            RefreshPayload(dashboard, assets, categories, schemas, dictionaries, changes, reports, reportCatalog)
        }.onSuccess { result ->
            if (_state.value.user?.id != userId || factory.tenantSlug != tenantSlug) return@onSuccess
            val sameFilters = filters == Triple(_state.value.assetQuery, _state.value.assetOs, _state.value.assetUnassigned)
            _state.value = _state.value.copy(
                loading = false,
                dashboard = result.dashboard,
                assets = if (sameFilters) result.assets.items else _state.value.assets,
                assetTotal = if (sameFilters) result.assets.total else _state.value.assetTotal,
                assetPage = if (sameFilters) 1 else _state.value.assetPage,
                people = result.dictionaries["osoba"].orEmpty(),
                dictionaryCategories = result.categories,
                dictionarySchemas = result.schemas,
                dictionaries = result.dictionaries,
                changes = result.changes,
                reports = result.reports,
                reportCatalog = result.reportCatalog,
            )
        }.onFailure { _state.value = _state.value.copy(loading = false, error = it.userMessage()) }
        // Zgloszenia sa osobnym zakresem danych i osobnym uprawnieniem, wiec
        // ida osobnym torem: konto bez helpdesku ma zobaczyc pulpit, a nie
        // blad dostepu do cudzej zakladki.
        odswiezHelpdesk()
    }

    private fun mutate(block: suspend (CmdbApi) -> Unit) = viewModelScope.launch {
        val service = api ?: return@launch
        if (_state.value.saving) return@launch
        _state.value = _state.value.copy(saving = true, error = null)
        try {
            block(service)
            _state.value = _state.value.copy(saving = false, mutationVersion = _state.value.mutationVersion + 1)
            refreshAll()
        } catch (error: CancellationException) { throw error }
        catch (error: Exception) { _state.value = _state.value.copy(saving = false, error = error.userMessage()) }
    }

    fun sendReport(id: String) { mutate { it.sendReport(id) } }
    fun saveReport(id: String?, body: ReportWrite) { mutate {
        if (id == null) it.createReport(body) else it.updateReport(id, body)
    } }
    fun deleteReport(id: String) { mutate { it.deleteReport(id) } }

    fun searchAssets(query: String, os: String, unassigned: Boolean) {
        assetSearch?.cancel()
        _state.value = _state.value.copy(assetQuery = query, assetOs = os, assetUnassigned = unassigned,
            assets = emptyList(), assetTotal = 0, assetLoading = true)
        assetSearch = viewModelScope.launch { delay(300); loadAssets(1) }
    }

    fun moreAssets() {
        if (_state.value.assetLoading) return
        assetSearch = viewModelScope.launch { loadAssets(_state.value.assetPage + 1) }
    }

    private suspend fun loadAssets(page: Int) {
        val service = api ?: return
        _state.value = _state.value.copy(assetLoading = true)
        try {
            val result = service.assets(query = _state.value.assetQuery, page = page,
                osFamily = _state.value.assetOs, unassigned = _state.value.assetUnassigned)
            _state.value = _state.value.copy(assetLoading = false, assetPage = page, assetTotal = result.total,
                assets = if (page == 1) result.items else (_state.value.assets + result.items).distinctBy { it.id })
        } catch (error: CancellationException) { throw error }
        catch (error: Exception) { _state.value = _state.value.copy(assetLoading = false, error = error.userMessage()) }
    }

    fun openAsset(id: String) = viewModelScope.launch {
        val service = api ?: return@launch
        _state.value = _state.value.copy(loading = true, error = null)
        runCatching { service.asset(id) }
            .onSuccess { _state.value = _state.value.copy(loading = false, selectedAsset = it) }
            .onFailure { _state.value = _state.value.copy(loading = false, error = it.userMessage()) }
    }

    fun closeAsset() { _state.value = _state.value.copy(selectedAsset = null) }

    fun saveDictionary(category: String, id: String?, values: Map<String, kotlinx.serialization.json.JsonElement>) {
        mutate { service ->
            if (id == null) service.createDictionaryEntry(category, DictionaryWrite(values))
            else service.updateDictionaryEntry(category, id, DictionaryWrite(values))
        }
    }
    fun deleteDictionary(category: String, id: String) { mutate { it.deleteDictionaryEntry(category, id) } }
    fun updateAssignment(assetId: String, body: AssignmentWrite) { mutate { service ->
        service.updateAssignment(assetId, body)
        val detail = service.asset(assetId)
        _state.value = _state.value.copy(selectedAsset = detail)
    } }

    // --- helpdesk -----------------------------------------------------------

    private var ticketSearch: Job? = null
    private val helpdeskJson = Json { encodeDefaults = true; explicitNulls = false }

    private fun helpdesk() = _state.value.helpdesk

    private fun ustawHelpdesk(zmiana: HelpdeskState.() -> HelpdeskState) {
        _state.value = _state.value.copy(helpdesk = _state.value.helpdesk.zmiana())
    }

    private suspend fun odswiezHelpdesk() {
        val service = api ?: return
        // Katalog odpowiada 200 takze kontu bez helpdesku (available = false),
        // wiec jego blad znaczy naprawde blad - i tylko wtedy go pokazujemy.
        val katalog = runCatching { service.helpdeskCatalog() }.getOrNull() ?: return
        ustawHelpdesk { copy(available = katalog.available, catalog = katalog) }
        if (katalog.available) pobierzZgloszenia(1)
    }

    private suspend fun pobierzZgloszenia(page: Int) {
        val service = api ?: return
        if (!helpdesk().available) return
        val filtry = helpdesk().query to helpdesk().scope
        ustawHelpdesk { copy(loading = true) }
        runCatching { service.tickets(query = filtry.first, scope = filtry.second, page = page) }
            .onSuccess { wynik ->
                // Odpowiedz na nieaktualny filtr nie ma prawa podmienic listy,
                // ktora widzi technik - przy pisaniu w polu szukania zapytania
                // wracaja nie po kolei.
                if (filtry != (helpdesk().query to helpdesk().scope)) return@onSuccess
                ustawHelpdesk {
                    copy(
                        loading = false, page = page, total = wynik.total,
                        counters = wynik.counters,
                        tickets = if (page == 1) wynik.items
                        else (tickets + wynik.items).distinctBy { it.id },
                    )
                }
            }
            .onFailure {
                ustawHelpdesk { copy(loading = false) }
                _state.value = _state.value.copy(error = it.userMessage())
            }
    }

    fun refreshHelpdesk() = viewModelScope.launch { odswiezHelpdesk() }

    fun searchTickets(query: String, scope: String) {
        ticketSearch?.cancel()
        ustawHelpdesk { copy(query = query, scope = scope, tickets = emptyList(), total = 0, loading = true) }
        ticketSearch = viewModelScope.launch { delay(300); pobierzZgloszenia(1) }
    }

    fun moreTickets() {
        if (helpdesk().loading) return
        ticketSearch = viewModelScope.launch { pobierzZgloszenia(helpdesk().page + 1) }
    }

    fun openTicket(id: String) = viewModelScope.launch {
        val service = api ?: return@launch
        ustawHelpdesk { copy(loading = true) }
        runCatching { service.ticket(id) }
            .onSuccess { ustawHelpdesk { copy(loading = false, detail = it, assetPicker = emptyList()) } }
            .onFailure {
                ustawHelpdesk { copy(loading = false) }
                _state.value = _state.value.copy(error = it.userMessage())
            }
    }

    fun closeTicket() { ustawHelpdesk { copy(detail = null, assetPicker = emptyList()) } }

    /**
     * Wspolna droga kazdego zapisu w helpdesku.
     *
     * Po udanym zapisie odswiezamy TYLKO helpdesk, a nie caly komplet danych
     * aplikacji: odpowiedz w watku nie jest powodem, zeby telefon pobieral od
     * nowa slowniki i raporty.
     */
    private fun zapiszHelpdesk(
        ticketId: String?, block: suspend (CmdbApi) -> TicketSaved,
    ) = viewModelScope.launch {
        val service = api ?: return@launch
        if (_state.value.saving) return@launch
        _state.value = _state.value.copy(saving = true, error = null, notice = null)
        try {
            val wynik = block(service)
            _state.value = _state.value.copy(
                saving = false,
                mutationVersion = _state.value.mutationVersion + 1,
                notice = wynik.detail.ifBlank { null },
            )
            (ticketId ?: wynik.id)?.let { openTicket(it).join() }
            odswiezHelpdesk()
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            _state.value = _state.value.copy(saving = false, error = error.userMessage())
        }
    }

    private fun czesciPlikow(pliki: List<WybranyPlik>): List<MultipartBody.Part> {
        val context = getApplication<Application>()
        return pliki.mapNotNull { context.czescPliku(it) }
    }

    private fun czescDanych(tekst: String): RequestBody =
        tekst.toRequestBody("application/json".toMediaType())

    fun createTicket(body: NewTicket, pliki: List<WybranyPlik>) = zapiszHelpdesk(null) { service ->
        val dane = czescDanych(helpdeskJson.encodeToString(NewTicket.serializer(), body))
        withContext(Dispatchers.IO) { service.createTicket(dane, czesciPlikow(pliki)) }
    }

    fun sendTicketMessage(id: String, body: NewMessage, pliki: List<WybranyPlik>) =
        zapiszHelpdesk(id) { service ->
            val dane = czescDanych(helpdeskJson.encodeToString(NewMessage.serializer(), body))
            withContext(Dispatchers.IO) { service.addTicketMessage(id, dane, czesciPlikow(pliki)) }
        }

    fun setTicketStatus(id: String, status: String, summary: String) = zapiszHelpdesk(id) {
        it.setTicketStatus(id, TicketStatusWrite(status, summary))
    }

    fun assignTicket(id: String, technicianId: String) = zapiszHelpdesk(id) {
        it.setTicketTechnician(id, TicketTechnicianWrite(technicianId))
    }

    fun addTicketTime(id: String, minutes: Int, description: String) = zapiszHelpdesk(id) {
        it.addTicketTime(id, TicketTimeWrite(minutes, description))
    }

    fun changeTicketAsset(id: String, assetId: String, action: String) = zapiszHelpdesk(id) {
        it.changeTicketAsset(id, TicketAssetWrite(assetId, action))
    }

    fun searchHelpdeskAssets(tenantId: String, query: String) = viewModelScope.launch {
        val service = api ?: return@launch
        runCatching { service.helpdeskAssets(tenant = tenantId, query = query) }
            .onSuccess { ustawHelpdesk { copy(assetPicker = it) } }
            .onFailure { _state.value = _state.value.copy(error = it.userMessage()) }
    }

    /**
     * Pobiera zalacznik i oddaje go aplikacji, ktora umie go otworzyc.
     *
     * Plik idzie przez API z tokenem sesji, wiec nie da sie go otworzyc samym
     * adresem w przegladarce - telefon musi go najpierw pobrac do katalogu
     * podrecznego.
     */
    fun openAttachment(attachment: TicketAttachment) = viewModelScope.launch {
        val service = api ?: return@launch
        val context = getApplication<Application>()
        runCatching {
            withContext(Dispatchers.IO) {
                val dane = service.attachment(attachment.id).use { it.bytes() }
                context.zapiszZalacznik(attachment.id, attachment.name, dane)
            }
        }.onSuccess { adres ->
            if (!context.otworzPlik(adres, attachment.mime)) {
                _state.value = _state.value.copy(
                    error = "Telefon nie ma czym otworzyć pliku ${attachment.name}.",
                )
            }
        }.onFailure { _state.value = _state.value.copy(error = it.userMessage()) }
    }

    fun clearError() { _state.value = _state.value.copy(error = null) }

    fun clearNotice() { _state.value = _state.value.copy(notice = null) }
}

@Composable
fun CmdbApp(vm: CmdbViewModel = viewModel()) {
    val state by vm.state.collectAsStateWithLifecycle()
    val systemDark = isSystemInDarkTheme()
    val context = LocalContext.current
    val preferences = remember { context.getSharedPreferences("cmdb_appearance", 0) }
    var theme by remember { mutableStateOf(preferences.getString("theme", "system") ?: "system") }
    val darkMode = if (theme == "system") systemDark else theme == "dark"
    CmdbVisualTheme(darkMode) {
        when {
            state.restoring -> ModernLoadingScreen()
            state.user == null -> ModernLoginScreen(
                state.rememberedServer, state.rememberedEmail,
                state.loading, state.error, vm::login, vm::clearError,
            )
            state.tenantRequired -> ModernTenantScreen(state, vm::selectTenant, vm::logout)
            else -> ModernMainScreen(state, vm::refreshAll, vm::logout, {
                theme = when (theme) { "system" -> "light"; "light" -> "dark"; else -> "system" }
                preferences.edit().putString("theme", theme).apply()
            }, theme,
                vm::sendReport, vm::saveReport, vm::deleteReport,
                vm::openAsset, vm::closeAsset, vm::saveDictionary, vm::deleteDictionary,
                vm::updateAssignment, vm::searchAssets, vm::moreAssets, vm::chooseTenant,
                vm::clearError, vm::clearNotice,
                HelpdeskActions(
                    onOpen = vm::openTicket,
                    onClose = vm::closeTicket,
                    onSearch = vm::searchTickets,
                    onMore = vm::moreTickets,
                    onRefresh = vm::refreshHelpdesk,
                    onCreate = vm::createTicket,
                    onSend = vm::sendTicketMessage,
                    onStatus = vm::setTicketStatus,
                    onAssign = vm::assignTicket,
                    onTime = vm::addTicketTime,
                    onAsset = vm::changeTicketAsset,
                    onSearchAssets = vm::searchHelpdeskAssets,
                    onOpenAttachment = vm::openAttachment,
                ))
        }
    }
}

@Composable private fun LoadingScreen() = Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
    CircularProgressIndicator()
}

@Composable
private fun LoginScreen(
    initialServer: String,
    initialEmail: String,
    loading: Boolean,
    error: String?,
    onLogin: (String, String, String) -> Unit,
    onErrorShown: () -> Unit,
) {
    var server by rememberSaveable(initialServer) { mutableStateOf(initialServer) }
    var email by rememberSaveable(initialEmail) { mutableStateOf(initialEmail) }
    var password by remember { mutableStateOf("") }
    val snackbar = remember { SnackbarHostState() }
    LaunchedEffect(error) { error?.let { snackbar.showSnackbar(it); onErrorShown() } }
    Scaffold(snackbarHost = { SnackbarHost(snackbar) }) { padding ->
        Column(
            Modifier.fillMaxSize().padding(padding).padding(24.dp),
            verticalArrangement = Arrangement.Center,
        ) {
            Text("CMDB", style = MaterialTheme.typography.displaySmall, fontWeight = FontWeight.Bold)
            Text("Bezpieczny dostęp do infrastruktury", color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(28.dp))
            OutlinedTextField(server, { server = it }, label = { Text("Adres portalu (HTTPS)") }, modifier = Modifier.fillMaxWidth(), singleLine = true)
            Spacer(Modifier.height(12.dp))
            OutlinedTextField(email, { email = it }, label = { Text("E-mail") }, modifier = Modifier.fillMaxWidth(), singleLine = true)
            Spacer(Modifier.height(12.dp))
            OutlinedTextField(password, { password = it }, label = { Text("Hasło") }, modifier = Modifier.fillMaxWidth(), visualTransformation = PasswordVisualTransformation(), singleLine = true)
            Spacer(Modifier.height(20.dp))
            Button(
                onClick = { onLogin(server, email, password) },
                enabled = !loading && server.isNotBlank() && email.isNotBlank() && password.isNotBlank(),
                modifier = Modifier.fillMaxWidth(),
            ) { if (loading) CircularProgressIndicator(Modifier.height(20.dp)) else Text("Zaloguj") }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun MainScreen(
    state: AppState,
    onRefresh: () -> Unit,
    onLogout: () -> Unit,
    onToggleTheme: () -> Unit,
    theme: String,
    onSendReport: (String) -> Unit,
    onSaveReport: (String?, ReportWrite) -> Unit,
    onDeleteReport: (String) -> Unit,
    onOpenAsset: (String) -> Unit,
    onCloseAsset: () -> Unit,
    onSaveDictionary: (String, String?, Map<String, kotlinx.serialization.json.JsonElement>) -> Unit,
    onDeleteDictionary: (String, String) -> Unit,
    onUpdateAssignment: (String, AssignmentWrite) -> Unit,
    onSearchAssets: (String, String, Boolean) -> Unit,
    onMoreAssets: () -> Unit,
    onChooseTenant: () -> Unit,
    onErrorShown: () -> Unit,
) {
    var section by remember { mutableStateOf(Section.DASHBOARD) }
    val snackbar = remember { SnackbarHostState() }
    if (state.selectedAsset != null) BackHandler(enabled = !state.saving, onBack = onCloseAsset)
    LaunchedEffect(state.error) { state.error?.let { snackbar.showSnackbar(it); onErrorShown() } }
    Scaffold(
        topBar = { TopAppBar(
            title = { Text(state.selectedAsset?.asset?.hostname ?: section.label) },
            navigationIcon = { if (state.selectedAsset != null) Button(enabled = !state.saving, onClick = onCloseAsset) { Text("Wstecz") } },
            actions = { if (state.selectedAsset == null) {
                if (state.tenants.size > 1) TextButton(
                    enabled = !state.saving && !state.loading,
                    onClick = onChooseTenant,
                ) { Text(state.user?.tenant?.name ?: "Firma") }
                TextButton(onClick = onToggleTheme) { Text(when (theme) { "dark" -> "Ciemny"; "light" -> "Jasny"; else -> "Systemowy" }) }
                TextButton(enabled = !state.saving && !state.loading, onClick = onLogout) { Text("Wyloguj") }
            } },
        ) },
        bottomBar = { if (state.selectedAsset == null) {
            NavigationBar {
                Section.entries.forEach { item ->
                    NavigationBarItem(
                        selected = section == item,
                        enabled = !state.saving,
                        onClick = { section = item },
                        icon = { Icon(item.icon, contentDescription = item.label) },
                        label = { Text(item.label) },
                    )
                }
            }
        } },
        snackbarHost = { SnackbarHost(snackbar) },
    ) { padding ->
        if (state.loading && state.dashboard == null) LoadingScreen()
        else if (state.selectedAsset != null) AssetDetailScreen(
            state.selectedAsset, state.dictionaries, state.user?.canWrite == true,
            padding, state.saving, state.mutationVersion, state.error, onUpdateAssignment,
        )
        else when (section) {
            Section.DASHBOARD -> DashboardScreen(state.dashboard, padding, onRefresh, onOpenAsset)
            Section.ASSETS -> AssetList(state, padding, onOpenAsset, onSearchAssets, onMoreAssets)
            Section.PEOPLE -> DictionariesScreen(
                state.dictionaryCategories, state.dictionarySchemas, state.dictionaries,
                state.user?.canWrite == true, padding, state.saving, state.mutationVersion, state.error, onSaveDictionary, onDeleteDictionary,
            )
            Section.CHANGES -> ChangesScreen(state.changes, padding)
            Section.REPORTS -> ReportManagementScreen(
                state.reports, state.reportCatalog, padding, state.user?.canWrite == true,
                state.saving, state.mutationVersion, state.error, onSendReport, onSaveReport, onDeleteReport,
            )
        }
    }
}

@Composable private fun DashboardScreen(data: Dashboard?, padding: PaddingValues, onRefresh: () -> Unit, onOpen: (String) -> Unit) {
    LazyColumn(Modifier.fillMaxSize().padding(padding), contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item { Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Metric("Maszyny", data?.total ?: 0, Modifier.weight(1f))
            Metric("Bez kontaktu", data?.stale ?: 0, Modifier.weight(1f))
            Metric("Bez opiekuna", data?.unassigned ?: 0, Modifier.weight(1f))
        } }
        item { Text("Ostatni kontakt", style = MaterialTheme.typography.titleLarge) }
        items(data?.recent.orEmpty(), key = { it.id }) { asset -> AssetRow(asset) { onOpen(asset.id) } }
        item { Button(onClick = onRefresh, modifier = Modifier.fillMaxWidth()) { Text("Odśwież") } }
    }
}

@Composable private fun Metric(label: String, value: Int, modifier: Modifier = Modifier) = Card(modifier) {
    Column(Modifier.padding(14.dp)) { Text(value.toString(), style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold); Text(label) }
}

@Composable private fun AssetList(state: AppState, padding: PaddingValues, onOpen: (String) -> Unit,
    onSearch: (String, String, Boolean) -> Unit, onMore: () -> Unit) {
    LazyColumn(Modifier.fillMaxSize().padding(padding), contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        item { OutlinedTextField(state.assetQuery, { onSearch(it, state.assetOs, state.assetUnassigned) },
            label = { Text("Nazwa, FQDN, IP lub numer seryjny") }, modifier = Modifier.fillMaxWidth(), singleLine = true) }
        item { LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            item { FilterChip(selected = state.assetOs.isEmpty(), onClick = { onSearch(state.assetQuery, "", state.assetUnassigned) }, label = { Text("Wszystkie systemy") }) }
            items(state.dashboard?.byOs.orEmpty().filter { !it.key.isNullOrBlank() }) { os ->
                FilterChip(selected = state.assetOs == os.key, onClick = { onSearch(state.assetQuery, os.key.orEmpty(), state.assetUnassigned) }, label = { Text(os.label) })
            }
            item { FilterChip(selected = state.assetUnassigned, onClick = { onSearch(state.assetQuery, state.assetOs, !state.assetUnassigned) }, label = { Text("Bez opiekuna") }) }
        } }
        item { Text("Wyświetlono ${state.assets.size} z ${state.assetTotal}") }
        items(state.assets, key = { it.id }) { AssetRow(it) { onOpen(it.id) } }
        if (state.assetLoading) item { CircularProgressIndicator() }
        else if (state.assets.isEmpty()) item { Text("Brak maszyn spełniających kryteria") }
        if (state.assets.size < state.assetTotal) item {
            Button(onClick = onMore, enabled = !state.assetLoading, modifier = Modifier.fillMaxWidth()) { Text("Załaduj kolejne") }
        }
    }
}

@Composable private fun AssetRow(asset: AssetSummary, onClick: () -> Unit) = Card(Modifier.fillMaxWidth().clickable(onClick = onClick)) {
    Column(Modifier.padding(14.dp)) {
        Text(asset.hostname, fontWeight = FontWeight.SemiBold)
        Text(listOfNotNull(asset.primaryIp, asset.osFamily, asset.owner?.value).joinToString(" • "), color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable private fun AssetDetailScreen(
    data: AssetDetail,
    dictionaries: Map<String, List<DictionaryEntry>>,
    canWrite: Boolean,
    padding: PaddingValues,
    saving: Boolean,
    mutationVersion: Int,
    error: String?,
    onSaveAssignment: (String, AssignmentWrite) -> Unit,
) {
    var editAssignment by remember(data.asset.id) { mutableStateOf(false) }
    LaunchedEffect(mutationVersion) { editAssignment = false }
    if (editAssignment) AssignmentDialog(data.asset, dictionaries, saving, error, { editAssignment = false }) {
        onSaveAssignment(data.asset.id, it)
    }
    LazyColumn(
    Modifier.fillMaxSize().padding(padding),
    contentPadding = PaddingValues(16.dp),
    verticalArrangement = Arrangement.spacedBy(12.dp),
) {
    item {
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text(data.asset.hostname, style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
                DetailLine("Adres IP", data.asset.primaryIp)
                DetailLine("System", data.asset.osFamily)
                DetailLine("Typ", data.asset.type)
                DetailLine("Źródło", data.asset.source)
                DetailLine("Opiekun", data.asset.owner?.value)
                DetailLine("Użytkownik", data.asset.user?.value)
                DetailLine("Lokalizacja", data.asset.location?.value)
                DetailLine("Rola", data.asset.roleLabel)
                DetailLine("Miejsce", data.asset.place)
                DetailLine("Ostatni kontakt", data.asset.lastSeen)
                if (canWrite) Button(onClick = { editAssignment = true }, modifier = Modifier.fillMaxWidth()) { Text("Edytuj przypisania") }
            }
        }
    }
    if (data.facts.isNotEmpty()) {
        item { Text("Podsumowanie sprzętu", style = MaterialTheme.typography.titleLarge) }
        items(data.facts.entries.toList(), key = { it.key }) { (key, value) ->
            Card(Modifier.fillMaxWidth()) { DetailLine(key, value.toString(), Modifier.padding(14.dp)) }
        }
    }
    if (data.attributes.isNotEmpty()) {
        item { Text("Dane dodatkowe", style = MaterialTheme.typography.titleLarge) }
        items(data.attributes.entries.toList(), key = { it.key }) { (key, value) ->
            Card(Modifier.fillMaxWidth()) { DetailLine(key, value.toString(), Modifier.padding(14.dp)) }
        }
    }
    data.currentReport?.let { report ->
        item { Text("Pełny raport agenta", style = MaterialTheme.typography.titleLarge) }
        items(report.entries.toList(), key = { it.key }) { (key, value) ->
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(14.dp)) {
                    Text(key, fontWeight = FontWeight.SemiBold)
                    Text(value.toString(), color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
    }
}
}

@Composable private fun DetailLine(label: String, value: String?, modifier: Modifier = Modifier) {
    if (!value.isNullOrBlank()) Row(modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(label, modifier = Modifier.weight(1f), color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(value, modifier = Modifier.weight(1f), fontWeight = FontWeight.Medium)
    }
}

@Composable private fun ChangesScreen(data: List<ChangeEntry>, padding: PaddingValues) = LazyColumn(
    Modifier.fillMaxSize().padding(padding), contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)
) { items(data, key = { it.id }) { Card(Modifier.fillMaxWidth()) { Column(Modifier.padding(14.dp)) { Text(it.hostname, fontWeight = FontWeight.SemiBold); Text("${it.action}: ${it.label}"); Text(it.occurredAt, color = MaterialTheme.colorScheme.onSurfaceVariant) } } } }
