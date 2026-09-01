package pl.hubzso.cmdb.ui

import android.app.Application
import androidx.compose.foundation.clickable
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
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Assessment
import androidx.compose.material.icons.outlined.Computer
import androidx.compose.material.icons.outlined.Dashboard
import androidx.compose.material.icons.outlined.History
import androidx.compose.material.icons.outlined.MenuBook
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import pl.hubzso.cmdb.data.ApiFactory
import pl.hubzso.cmdb.data.AssetSummary
import pl.hubzso.cmdb.data.ChangeEntry
import pl.hubzso.cmdb.data.CmdbApi
import pl.hubzso.cmdb.data.Dashboard
import pl.hubzso.cmdb.data.DictionaryEntry
import pl.hubzso.cmdb.data.LoginRequest
import pl.hubzso.cmdb.data.ReportDefinition
import pl.hubzso.cmdb.data.SessionStore
import pl.hubzso.cmdb.data.User
import pl.hubzso.cmdb.data.userMessage

private enum class Section(val label: String, val icon: ImageVector) {
    DASHBOARD("Pulpit", Icons.Outlined.Dashboard),
    ASSETS("Maszyny", Icons.Outlined.Computer),
    PEOPLE("Osoby", Icons.Outlined.MenuBook),
    CHANGES("Zmiany", Icons.Outlined.History),
    REPORTS("Raporty", Icons.Outlined.Assessment),
}

data class AppState(
    val restoring: Boolean = true,
    val loading: Boolean = false,
    val user: User? = null,
    val dashboard: Dashboard? = null,
    val assets: List<AssetSummary> = emptyList(),
    val people: List<DictionaryEntry> = emptyList(),
    val changes: List<ChangeEntry> = emptyList(),
    val reports: List<ReportDefinition> = emptyList(),
    val error: String? = null,
)

class CmdbViewModel(application: Application) : AndroidViewModel(application) {
    private val session = SessionStore(application)
    private val factory = ApiFactory(session)
    private var api: CmdbApi? = null
    private val _state = MutableStateFlow(AppState())
    val state: StateFlow<AppState> = _state.asStateFlow()

    init {
        viewModelScope.launch {
            session.restore()
            val server = session.serverUrl
            if (!server.isNullOrBlank() && !session.token.isNullOrBlank()) {
                runCatching {
                    api = factory.create(server)
                    api!!.me()
                }.onSuccess { user ->
                    _state.value = AppState(restoring = false, user = user)
                    refreshAll()
                }.onFailure {
                    session.clear()
                    _state.value = AppState(restoring = false, error = it.userMessage())
                }
            } else {
                _state.value = AppState(restoring = false)
            }
        }
    }

    fun login(server: String, email: String, password: String) = viewModelScope.launch {
        _state.value = _state.value.copy(loading = true, error = null)
        runCatching {
            require(server.trim().startsWith("https://")) { "Adres musi rozpoczynać się od https://" }
            val temporary = factory.create(server)
            val result = temporary.login(LoginRequest(email.trim(), password))
            session.save(server, result.accessToken)
            api = factory.create(server)
            result.user
        }.onSuccess {
            _state.value = AppState(restoring = false, user = it)
            refreshAll()
        }.onFailure {
            _state.value = _state.value.copy(loading = false, error = it.userMessage())
        }
    }

    fun logout() = viewModelScope.launch {
        session.clear()
        api = null
        _state.value = AppState(restoring = false)
    }

    fun refreshAll() = viewModelScope.launch {
        val service = api ?: return@launch
        _state.value = _state.value.copy(loading = true, error = null)
        runCatching {
            val dashboard = service.dashboard()
            val assets = service.assets(pageSize = 100).items
            val people = service.dictionary("osoba")
            val changes = service.changes()
            val reports = service.reports()
            listOf(dashboard, assets, people, changes, reports)
        }.onSuccess { result ->
            @Suppress("UNCHECKED_CAST")
            _state.value = _state.value.copy(
                loading = false,
                dashboard = result[0] as Dashboard,
                assets = result[1] as List<AssetSummary>,
                people = result[2] as List<DictionaryEntry>,
                changes = result[3] as List<ChangeEntry>,
                reports = result[4] as List<ReportDefinition>,
            )
        }.onFailure { _state.value = _state.value.copy(loading = false, error = it.userMessage()) }
    }

    fun sendReport(id: String) = viewModelScope.launch {
        runCatching { api?.sendReport(id) ?: error("Brak połączenia") }
            .onFailure { _state.value = _state.value.copy(error = it.userMessage()) }
            .onSuccess { refreshAll() }
    }

    fun clearError() { _state.value = _state.value.copy(error = null) }
}

@Composable
fun CmdbApp(vm: CmdbViewModel = viewModel()) {
    val state by vm.state.collectAsStateWithLifecycle()
    MaterialTheme {
        when {
            state.restoring -> LoadingScreen()
            state.user == null -> LoginScreen(state.loading, state.error, vm::login, vm::clearError)
            else -> MainScreen(state, vm::refreshAll, vm::logout, vm::sendReport, vm::clearError)
        }
    }
}

@Composable private fun LoadingScreen() = Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
    CircularProgressIndicator()
}

@Composable
private fun LoginScreen(
    loading: Boolean,
    error: String?,
    onLogin: (String, String, String) -> Unit,
    onErrorShown: () -> Unit,
) {
    var server by remember { mutableStateOf("") }
    var email by remember { mutableStateOf("") }
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
    onSendReport: (String) -> Unit,
    onErrorShown: () -> Unit,
) {
    var section by remember { mutableStateOf(Section.DASHBOARD) }
    val snackbar = remember { SnackbarHostState() }
    LaunchedEffect(state.error) { state.error?.let { snackbar.showSnackbar(it); onErrorShown() } }
    Scaffold(
        topBar = { TopAppBar(title = { Text(section.label) }, actions = { Button(onClick = onLogout) { Text("Wyloguj") } }) },
        bottomBar = {
            NavigationBar {
                Section.entries.forEach { item ->
                    NavigationBarItem(
                        selected = section == item,
                        onClick = { section = item },
                        icon = { Icon(item.icon, contentDescription = item.label) },
                        label = { Text(item.label) },
                    )
                }
            }
        },
        snackbarHost = { SnackbarHost(snackbar) },
    ) { padding ->
        if (state.loading && state.dashboard == null) LoadingScreen()
        else when (section) {
            Section.DASHBOARD -> DashboardScreen(state.dashboard, padding, onRefresh)
            Section.ASSETS -> AssetList(state.assets, padding)
            Section.PEOPLE -> PeopleScreen(state.people, padding)
            Section.CHANGES -> ChangesScreen(state.changes, padding)
            Section.REPORTS -> ReportsScreen(state.reports, padding, state.user?.canWrite == true, onSendReport)
        }
    }
}

@Composable private fun DashboardScreen(data: Dashboard?, padding: PaddingValues, onRefresh: () -> Unit) {
    LazyColumn(Modifier.fillMaxSize().padding(padding), contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item { Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Metric("Maszyny", data?.total ?: 0, Modifier.weight(1f))
            Metric("Bez kontaktu", data?.stale ?: 0, Modifier.weight(1f))
            Metric("Bez opiekuna", data?.unassigned ?: 0, Modifier.weight(1f))
        } }
        item { Text("Ostatni kontakt", style = MaterialTheme.typography.titleLarge) }
        items(data?.recent.orEmpty(), key = { it.id }) { AssetRow(it) }
        item { Button(onClick = onRefresh, modifier = Modifier.fillMaxWidth()) { Text("Odśwież") } }
    }
}

@Composable private fun Metric(label: String, value: Int, modifier: Modifier = Modifier) = Card(modifier) {
    Column(Modifier.padding(14.dp)) { Text(value.toString(), style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold); Text(label) }
}

@Composable private fun AssetList(data: List<AssetSummary>, padding: PaddingValues) = LazyColumn(
    Modifier.fillMaxSize().padding(padding), contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)
) { items(data, key = { it.id }) { AssetRow(it) } }

@Composable private fun AssetRow(asset: AssetSummary) = Card(Modifier.fillMaxWidth().clickable { }) {
    Column(Modifier.padding(14.dp)) {
        Text(asset.hostname, fontWeight = FontWeight.SemiBold)
        Text(listOfNotNull(asset.primaryIp, asset.osFamily, asset.owner?.value).joinToString(" • "), color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable private fun PeopleScreen(data: List<DictionaryEntry>, padding: PaddingValues) = LazyColumn(
    Modifier.fillMaxSize().padding(padding), contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)
) { items(data, key = { it.id }) { Card(Modifier.fillMaxWidth()) { Column(Modifier.padding(14.dp)) { Text(it.value, fontWeight = FontWeight.SemiBold); Text(it.attributes.values.joinToString(" • ")) } } } }

@Composable private fun ChangesScreen(data: List<ChangeEntry>, padding: PaddingValues) = LazyColumn(
    Modifier.fillMaxSize().padding(padding), contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)
) { items(data, key = { it.id }) { Card(Modifier.fillMaxWidth()) { Column(Modifier.padding(14.dp)) { Text(it.hostname, fontWeight = FontWeight.SemiBold); Text("${it.action}: ${it.label}"); Text(it.occurredAt, color = MaterialTheme.colorScheme.onSurfaceVariant) } } } }

@Composable private fun ReportsScreen(data: List<ReportDefinition>, padding: PaddingValues, canWrite: Boolean, onSend: (String) -> Unit) = LazyColumn(
    Modifier.fillMaxSize().padding(padding), contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)
) { items(data, key = { it.id }) { report -> Card(Modifier.fillMaxWidth()) { Column(Modifier.padding(14.dp)) { Text(report.name, fontWeight = FontWeight.SemiBold); Text("${report.type} • ${report.frequency}"); Text(report.recipients); if (canWrite) Button(onClick = { onSend(report.id) }) { Text("Wyślij teraz") } } } } }
