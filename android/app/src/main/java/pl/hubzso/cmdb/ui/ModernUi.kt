package pl.hubzso.cmdb.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.ArrowBack
import androidx.compose.material.icons.outlined.Add
import androidx.compose.material.icons.outlined.Assessment
import androidx.compose.material.icons.outlined.Badge
import androidx.compose.material.icons.outlined.Business
import androidx.compose.material.icons.outlined.ChevronRight
import androidx.compose.material.icons.outlined.Computer
import androidx.compose.material.icons.outlined.Dashboard
import androidx.compose.material.icons.outlined.DarkMode
import androidx.compose.material.icons.outlined.Dns
import androidx.compose.material.icons.outlined.Email
import androidx.compose.material.icons.outlined.FilterList
import androidx.compose.material.icons.outlined.History
import androidx.compose.material.icons.outlined.Home
import androidx.compose.material.icons.outlined.KeyboardArrowDown
import androidx.compose.material.icons.outlined.LightMode
import androidx.compose.material.icons.outlined.LocationOn
import androidx.compose.material.icons.outlined.Lock
import androidx.compose.material.icons.outlined.Logout
import androidx.compose.material.icons.outlined.Memory
import androidx.compose.material.icons.outlined.MenuBook
import androidx.compose.material.icons.outlined.MoreVert
import androidx.compose.material.icons.outlined.Person
import androidx.compose.material.icons.outlined.PersonOff
import androidx.compose.material.icons.outlined.Refresh
import androidx.compose.material.icons.outlined.Save
import androidx.compose.material.icons.outlined.Search
import androidx.compose.material.icons.outlined.Settings
import androidx.compose.material.icons.outlined.Storage
import androidx.compose.material.icons.outlined.SyncProblem
import androidx.compose.material.icons.outlined.Visibility
import androidx.compose.material.icons.outlined.VisibilityOff
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import pl.hubzso.cmdb.data.AssetDetail
import pl.hubzso.cmdb.data.AssetSummary
import pl.hubzso.cmdb.data.AssignmentWrite
import pl.hubzso.cmdb.data.ChangeEntry
import pl.hubzso.cmdb.data.CountItem
import pl.hubzso.cmdb.data.Dashboard
import pl.hubzso.cmdb.data.DictionaryEntry
import pl.hubzso.cmdb.data.ReportWrite

internal val BrandNavy = Color(0xFF073B78)
internal val BrandBlue = Color(0xFF0877E1)
internal val BrandCyan = Color(0xFF17A6FF)
internal val SuccessGreen = Color(0xFF20B15A)
internal val WarningAmber = Color(0xFFF5A623)
internal val DangerRed = Color(0xFFE84C4F)
private val LightBackground = Color(0xFFF4F7FB)
private val DarkBackground = Color(0xFF071725)
private val DarkSurface = Color(0xFF102438)

private val CmdbLightColors = lightColorScheme(
    primary = BrandBlue,
    onPrimary = Color.White,
    primaryContainer = Color(0xFFE5F1FF),
    onPrimaryContainer = BrandNavy,
    secondary = BrandCyan,
    background = LightBackground,
    surface = Color.White,
    surfaceVariant = Color(0xFFEAF0F7),
    onSurface = Color(0xFF17212D),
    onSurfaceVariant = Color(0xFF667487),
    outline = Color(0xFFB8C3D0),
    error = DangerRed,
)

private val CmdbDarkColors = darkColorScheme(
    primary = Color(0xFF4AA3FF),
    onPrimary = Color.White,
    primaryContainer = Color(0xFF123D68),
    onPrimaryContainer = Color(0xFFD9EAFF),
    secondary = Color(0xFF5BC0FF),
    background = DarkBackground,
    surface = DarkSurface,
    surfaceVariant = Color(0xFF173047),
    onSurface = Color(0xFFF3F7FC),
    onSurfaceVariant = Color(0xFFAAB8C8),
    outline = Color(0xFF53677C),
    error = Color(0xFFFF7478),
)

@Composable
internal fun CmdbVisualTheme(darkMode: Boolean, content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = if (darkMode) CmdbDarkColors else CmdbLightColors,
        typography = MaterialTheme.typography.copy(
            headlineMedium = MaterialTheme.typography.headlineMedium.copy(fontWeight = FontWeight.Bold),
            titleLarge = MaterialTheme.typography.titleLarge.copy(fontWeight = FontWeight.SemiBold),
            titleMedium = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.SemiBold),
        ),
        shapes = MaterialTheme.shapes.copy(
            small = RoundedCornerShape(8.dp),
            medium = RoundedCornerShape(14.dp),
            large = RoundedCornerShape(20.dp),
        ),
        content = content,
    )
}

@Composable
internal fun ModernLoadingScreen() = Box(
    Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background),
    contentAlignment = Alignment.Center,
) { CircularProgressIndicator(color = MaterialTheme.colorScheme.primary) }

@Composable
internal fun ModernLoginScreen(
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
    var passwordVisible by remember { mutableStateOf(false) }
    val snackbar = remember { SnackbarHostState() }
    LaunchedEffect(error) { error?.let { snackbar.showSnackbar(it); onErrorShown() } }
    Scaffold(
        containerColor = MaterialTheme.colorScheme.background,
        snackbarHost = { SnackbarHost(snackbar) },
    ) { padding ->
        Box(Modifier.fillMaxSize().padding(padding), contentAlignment = Alignment.Center) {
            Column(
                Modifier.fillMaxWidth().padding(horizontal = 28.dp).verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.Center,
            ) {
                Text(
                    "CMDB",
                    color = MaterialTheme.colorScheme.primary,
                    fontSize = 48.sp,
                    lineHeight = 52.sp,
                    fontWeight = FontWeight.ExtraBold,
                )
                Text(
                    "Bezpieczny dostęp do infrastruktury",
                    style = MaterialTheme.typography.titleMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.height(34.dp))
                LoginLabel("ADRES PORTALU")
                ModernInput(
                    value = server,
                    onValueChange = { server = it },
                    placeholder = "https://cmdb.twojadomena.pl",
                    icon = Icons.Outlined.Business,
                    keyboardType = KeyboardType.Uri,
                )
                Spacer(Modifier.height(16.dp))
                LoginLabel("E-MAIL")
                ModernInput(
                    value = email,
                    onValueChange = { email = it },
                    placeholder = "twoj@email.pl",
                    icon = Icons.Outlined.Email,
                    keyboardType = KeyboardType.Email,
                )
                Spacer(Modifier.height(16.dp))
                LoginLabel("HASŁO")
                ModernInput(
                    value = password,
                    onValueChange = { password = it },
                    placeholder = "Wpisz hasło",
                    icon = Icons.Outlined.Lock,
                    visualTransformation = if (passwordVisible) VisualTransformation.None else PasswordVisualTransformation(),
                    trailing = {
                        IconButton(onClick = { passwordVisible = !passwordVisible }) {
                            Icon(
                                if (passwordVisible) Icons.Outlined.VisibilityOff else Icons.Outlined.Visibility,
                                contentDescription = if (passwordVisible) "Ukryj hasło" else "Pokaż hasło",
                            )
                        }
                    },
                )
                Spacer(Modifier.height(28.dp))
                GradientActionButton(
                    text = "ZALOGUJ SIĘ",
                    loading = loading,
                    enabled = !loading && server.isNotBlank() && email.isNotBlank() && password.isNotBlank(),
                ) { onLogin(server, email, password) }
                Spacer(Modifier.height(18.dp))
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center, verticalAlignment = Alignment.CenterVertically) {
                    Icon(Icons.Outlined.Lock, null, Modifier.size(14.dp), MaterialTheme.colorScheme.onSurfaceVariant)
                    Spacer(Modifier.width(6.dp))
                    Text("Połączenie szyfrowane przez HTTPS", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
    }
}

@Composable private fun LoginLabel(text: String) {
    Text(text, style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.Bold, color = MaterialTheme.colorScheme.onSurfaceVariant)
    Spacer(Modifier.height(6.dp))
}

@Composable private fun ModernInput(
    value: String,
    onValueChange: (String) -> Unit,
    placeholder: String,
    icon: ImageVector,
    keyboardType: KeyboardType = KeyboardType.Text,
    visualTransformation: VisualTransformation = VisualTransformation.None,
    trailing: @Composable (() -> Unit)? = null,
) {
    OutlinedTextField(
        value = value,
        onValueChange = onValueChange,
        modifier = Modifier.fillMaxWidth(),
        placeholder = { Text(placeholder) },
        leadingIcon = { Icon(icon, null, tint = MaterialTheme.colorScheme.primary) },
        trailingIcon = trailing,
        singleLine = true,
        shape = RoundedCornerShape(10.dp),
        keyboardOptions = KeyboardOptions(keyboardType = keyboardType),
        visualTransformation = visualTransformation,
        colors = OutlinedTextFieldDefaults.colors(
            focusedContainerColor = MaterialTheme.colorScheme.surface,
            unfocusedContainerColor = MaterialTheme.colorScheme.surface,
            focusedBorderColor = MaterialTheme.colorScheme.primary,
            unfocusedBorderColor = MaterialTheme.colorScheme.outline,
        ),
    )
}

@Composable private fun GradientActionButton(text: String, loading: Boolean, enabled: Boolean, onClick: () -> Unit) {
    val gradient = if (enabled) Brush.horizontalGradient(listOf(BrandBlue, BrandCyan))
        else Brush.horizontalGradient(listOf(MaterialTheme.colorScheme.outline, MaterialTheme.colorScheme.outline))
    Box(
        Modifier.fillMaxWidth().height(54.dp).clip(RoundedCornerShape(10.dp)).background(gradient).clickable(enabled = enabled, onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        if (loading) CircularProgressIndicator(Modifier.size(22.dp), color = Color.White, strokeWidth = 2.dp)
        else Text(text, color = Color.White, fontWeight = FontWeight.Bold, letterSpacing = 0.6.sp)
    }
}

@Composable
internal fun ModernTenantScreen(state: AppState, onSelect: (pl.hubzso.cmdb.data.Tenant) -> Unit, onLogout: () -> Unit) {
    Scaffold(containerColor = MaterialTheme.colorScheme.background) { padding ->
        LazyColumn(
            Modifier.fillMaxSize().padding(padding),
            contentPadding = PaddingValues(24.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            item { Text("Wybierz firmę", style = MaterialTheme.typography.headlineMedium) }
            item { Text("Dane aplikacji zostaną ograniczone do wybranej organizacji.", color = MaterialTheme.colorScheme.onSurfaceVariant) }
            items(state.tenants, key = { it.id }) { tenant ->
                ElevatedCmdbCard(Modifier.clickable { onSelect(tenant) }) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        RoundIcon(Icons.Outlined.Business, MaterialTheme.colorScheme.primary)
                        Spacer(Modifier.width(14.dp))
                        Text(tenant.name, Modifier.weight(1f), style = MaterialTheme.typography.titleMedium)
                        Icon(Icons.Outlined.ChevronRight, null, tint = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
            if (state.tenants.isEmpty()) item { Text("Brak dostępnych firm") }
            state.error?.let { item { Text(it, color = MaterialTheme.colorScheme.error) } }
            item { TextButton(onClick = onLogout) { Icon(Icons.Outlined.Logout, null); Spacer(Modifier.width(6.dp)); Text("Wyloguj") } }
        }
    }
}

private enum class ModernSection(val label: String, val icon: ImageVector) {
    DASHBOARD("Pulpit", Icons.Outlined.Dashboard),
    ASSETS("Maszyny", Icons.Outlined.Computer),
    PEOPLE("Osoby", Icons.Outlined.Person),
    CHANGES("Zmiany", Icons.Outlined.History),
    REPORTS("Raporty", Icons.Outlined.Assessment),
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun ModernMainScreen(
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
    onSaveDictionary: (String, String?, Map<String, JsonElement>) -> Unit,
    onDeleteDictionary: (String, String) -> Unit,
    onUpdateAssignment: (String, AssignmentWrite) -> Unit,
    onSearchAssets: (String, String, Boolean) -> Unit,
    onMoreAssets: () -> Unit,
    onChooseTenant: () -> Unit,
    onErrorShown: () -> Unit,
) {
    var section by rememberSaveable { mutableStateOf(ModernSection.DASHBOARD) }
    var menuOpen by remember { mutableStateOf(false) }
    val snackbar = remember { SnackbarHostState() }
    if (state.selectedAsset != null) BackHandler(enabled = !state.saving, onBack = onCloseAsset)
    LaunchedEffect(state.error) { state.error?.let { snackbar.showSnackbar(it); onErrorShown() } }
    Scaffold(
        containerColor = MaterialTheme.colorScheme.background,
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text(state.selectedAsset?.asset?.hostname ?: section.label, fontWeight = FontWeight.Bold)
                        if (state.selectedAsset == null && section == ModernSection.DASHBOARD) {
                            Text(state.user?.tenant?.name.orEmpty(), style = MaterialTheme.typography.labelSmall, color = Color.White.copy(alpha = .78f))
                        }
                    }
                },
                navigationIcon = {
                    if (state.selectedAsset != null) IconButton(enabled = !state.saving, onClick = onCloseAsset) {
                        Icon(Icons.AutoMirrored.Outlined.ArrowBack, "Wstecz")
                    }
                },
                actions = {
                    if (state.selectedAsset == null) {
                        IconButton(onClick = onRefresh, enabled = !state.loading && !state.saving) { Icon(Icons.Outlined.Refresh, "Odśwież") }
                        Box {
                            IconButton(onClick = { menuOpen = true }) { Icon(Icons.Outlined.MoreVert, "Więcej") }
                            DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
                                if (state.tenants.size > 1) DropdownMenuItem(
                                    text = { Text("Zmień firmę") },
                                    leadingIcon = { Icon(Icons.Outlined.Business, null) },
                                    onClick = { menuOpen = false; onChooseTenant() },
                                )
                                DropdownMenuItem(
                                    text = { Text("Motyw: ${when (theme) { "dark" -> "ciemny"; "light" -> "jasny"; else -> "systemowy" }}") },
                                    leadingIcon = { Icon(if (theme == "dark") Icons.Outlined.DarkMode else Icons.Outlined.LightMode, null) },
                                    onClick = { menuOpen = false; onToggleTheme() },
                                )
                                DropdownMenuItem(
                                    text = { Text("Wyloguj") },
                                    leadingIcon = { Icon(Icons.Outlined.Logout, null) },
                                    onClick = { menuOpen = false; onLogout() },
                                )
                            }
                        }
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = BrandNavy,
                    titleContentColor = Color.White,
                    navigationIconContentColor = Color.White,
                    actionIconContentColor = Color.White,
                ),
            )
        },
        bottomBar = {
            if (state.selectedAsset == null) NavigationBar(
                modifier = Modifier.shadow(10.dp),
                containerColor = MaterialTheme.colorScheme.surface,
            ) {
                ModernSection.entries.forEach { item ->
                    NavigationBarItem(
                        selected = section == item,
                        enabled = !state.saving,
                        onClick = { section = item },
                        icon = { Icon(item.icon, item.label) },
                        label = { Text(item.label, fontSize = 10.sp, maxLines = 1) },
                        colors = NavigationBarItemDefaults.colors(
                            selectedIconColor = MaterialTheme.colorScheme.primary,
                            selectedTextColor = MaterialTheme.colorScheme.primary,
                            indicatorColor = MaterialTheme.colorScheme.primaryContainer,
                        ),
                    )
                }
            }
        },
        snackbarHost = { SnackbarHost(snackbar) },
    ) { padding ->
        if (state.loading && state.dashboard == null) ModernLoadingScreen()
        else if (state.selectedAsset != null) ModernAssetDetailScreen(
            state.selectedAsset, state.dictionaries, state.user?.canWrite == true,
            padding, state.saving, state.mutationVersion, onUpdateAssignment,
        )
        else when (section) {
            ModernSection.DASHBOARD -> ModernDashboardScreen(state.dashboard, padding, onOpenAsset)
            ModernSection.ASSETS -> ModernAssetList(state, padding, onOpenAsset, onSearchAssets, onMoreAssets)
            ModernSection.PEOPLE -> VisualDictionariesScreen(
                state.dictionaryCategories, state.dictionarySchemas, state.dictionaries,
                state.user?.canWrite == true, padding, state.saving, state.mutationVersion,
                state.error, onSaveDictionary, onDeleteDictionary,
            )
            ModernSection.CHANGES -> ModernChangesScreen(state.changes, padding)
            ModernSection.REPORTS -> VisualReportManagementScreen(
                state.reports, state.reportCatalog, padding, state.user?.canWrite == true,
                state.saving, state.mutationVersion, state.error, onSendReport, onSaveReport, onDeleteReport,
            )
        }
    }
}

@Composable private fun ModernDashboardScreen(data: Dashboard?, padding: PaddingValues, onOpen: (String) -> Unit) {
    LazyColumn(
        Modifier.fillMaxSize().padding(padding),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(18.dp),
    ) {
        item {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                KpiCard("Maszyny", data?.total ?: 0, Icons.Outlined.Computer, BrandBlue, Modifier.weight(1f))
                KpiCard("Bez kontaktu", data?.stale ?: 0, Icons.Outlined.SyncProblem, WarningAmber, Modifier.weight(1f))
                KpiCard("Bez opiekuna", data?.unassigned ?: 0, Icons.Outlined.PersonOff, DangerRed, Modifier.weight(1f))
            }
        }
        if (!data?.byOs.isNullOrEmpty()) item {
            SectionHeading("Rozkład systemów")
            Spacer(Modifier.height(8.dp))
            ElevatedCmdbCard {
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    OsDonut(data!!.byOs, Modifier.size(138.dp))
                    Spacer(Modifier.width(20.dp))
                    Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                        data.byOs.take(5).forEachIndexed { index, item -> LegendRow(item, chartColors[index % chartColors.size]) }
                    }
                }
            }
        }
        item { SectionHeading("Ostatni kontakt") }
        items(data?.recent.orEmpty(), key = { it.id }) { asset -> ModernAssetCard(asset) { onOpen(asset.id) } }
        if (data?.recent.isNullOrEmpty()) item { EmptyState("Brak ostatnio widzianych urządzeń") }
    }
}

@Composable private fun KpiCard(label: String, value: Int, icon: ImageVector, color: Color, modifier: Modifier) {
    Card(modifier, shape = RoundedCornerShape(14.dp), colors = CardDefaults.cardColors(containerColor = color)) {
        Column(Modifier.padding(horizontal = 12.dp, vertical = 14.dp)) {
            Icon(icon, null, tint = Color.White, modifier = Modifier.size(24.dp))
            Spacer(Modifier.height(8.dp))
            Text(value.toString(), color = Color.White, style = MaterialTheme.typography.headlineMedium)
            Text(label, color = Color.White.copy(alpha = .88f), style = MaterialTheme.typography.labelSmall, maxLines = 1)
        }
    }
}

private val chartColors = listOf(BrandBlue, BrandCyan, SuccessGreen, WarningAmber, DangerRed, Color(0xFF8B6CE5))

@Composable private fun OsDonut(items: List<CountItem>, modifier: Modifier) {
    val total = items.sumOf { it.count }.coerceAtLeast(1)
    Box(modifier, contentAlignment = Alignment.Center) {
        Canvas(Modifier.fillMaxSize().padding(8.dp)) {
            var start = -90f
            items.forEachIndexed { index, item ->
                val sweep = 360f * item.count / total
                drawArc(chartColors[index % chartColors.size], start, (sweep - 3f).coerceAtLeast(1f), false, style = Stroke(17.dp.toPx(), cap = StrokeCap.Round))
                start += sweep
            }
        }
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text(total.toString(), style = MaterialTheme.typography.headlineMedium)
            Text("urządzeń", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable private fun LegendRow(item: CountItem, color: Color) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(9.dp).clip(CircleShape).background(color))
        Spacer(Modifier.width(8.dp))
        Text(item.label, Modifier.weight(1f), style = MaterialTheme.typography.bodyMedium, maxLines = 1, overflow = TextOverflow.Ellipsis)
        Text(item.count.toString(), fontWeight = FontWeight.Bold)
    }
}

@Composable private fun ModernAssetList(
    state: AppState,
    padding: PaddingValues,
    onOpen: (String) -> Unit,
    onSearch: (String, String, Boolean) -> Unit,
    onMore: () -> Unit,
) {
    LazyColumn(
        Modifier.fillMaxSize().padding(padding),
        contentPadding = PaddingValues(14.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item {
            OutlinedTextField(
                value = state.assetQuery,
                onValueChange = { onSearch(it, state.assetOs, state.assetUnassigned) },
                modifier = Modifier.fillMaxWidth(),
                placeholder = { Text("Szukaj maszyny, IP lub numeru seryjnego") },
                leadingIcon = { Icon(Icons.Outlined.Search, null) },
                trailingIcon = { Icon(Icons.Outlined.FilterList, null, tint = MaterialTheme.colorScheme.primary) },
                singleLine = true,
                shape = RoundedCornerShape(14.dp),
                colors = OutlinedTextFieldDefaults.colors(
                    focusedContainerColor = MaterialTheme.colorScheme.surface,
                    unfocusedContainerColor = MaterialTheme.colorScheme.surface,
                    unfocusedBorderColor = Color.Transparent,
                ),
            )
        }
        item {
            LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                item { CmdbFilterChip("Wszystkie", state.assetOs.isEmpty() && !state.assetUnassigned) { onSearch(state.assetQuery, "", false) } }
                items(state.dashboard?.byOs.orEmpty().filter { !it.key.isNullOrBlank() }) { os ->
                    CmdbFilterChip(os.label, state.assetOs == os.key) { onSearch(state.assetQuery, os.key.orEmpty(), state.assetUnassigned) }
                }
                item { CmdbFilterChip("Bez opiekuna", state.assetUnassigned) { onSearch(state.assetQuery, state.assetOs, !state.assetUnassigned) } }
            }
        }
        item { Text("${state.assetTotal} urządzeń", style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.onSurfaceVariant) }
        items(state.assets, key = { it.id }) { asset -> ModernAssetCard(asset) { onOpen(asset.id) } }
        if (state.assetLoading) item { Box(Modifier.fillMaxWidth().padding(16.dp), contentAlignment = Alignment.Center) { CircularProgressIndicator() } }
        else if (state.assets.isEmpty()) item { EmptyState("Brak maszyn spełniających kryteria") }
        if (state.assets.size < state.assetTotal) item {
            OutlinedButton(onClick = onMore, enabled = !state.assetLoading, modifier = Modifier.fillMaxWidth()) { Text("Załaduj kolejne") }
        }
    }
}

@Composable internal fun CmdbFilterChip(label: String, selected: Boolean, onClick: () -> Unit) {
    FilterChip(
        selected = selected,
        onClick = onClick,
        label = { Text(label) },
        shape = RoundedCornerShape(18.dp),
        colors = FilterChipDefaults.filterChipColors(
            selectedContainerColor = MaterialTheme.colorScheme.primary,
            selectedLabelColor = Color.White,
        ),
    )
}

@Composable private fun ModernAssetCard(asset: AssetSummary, onClick: () -> Unit) {
    ElevatedCmdbCard(Modifier.clickable(onClick = onClick)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(48.dp).clip(CircleShape).background(osColor(asset.osFamily).copy(alpha = .14f)), contentAlignment = Alignment.Center) {
                Icon(Icons.Outlined.Computer, null, tint = osColor(asset.osFamily), modifier = Modifier.size(25.dp))
            }
            Spacer(Modifier.width(13.dp))
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(asset.hostname, Modifier.weight(1f), style = MaterialTheme.typography.titleMedium, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    Box(Modifier.size(8.dp).clip(CircleShape).background(assetStatusColor(asset)))
                }
                Text(listOfNotNull(asset.primaryIp, asset.osFamily).joinToString(" • "), style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                val owner = asset.owner?.value ?: "Brak opiekuna"
                Text(owner, style = MaterialTheme.typography.bodySmall, color = if (asset.owner == null) DangerRed else MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Icon(Icons.Outlined.ChevronRight, null, tint = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

private fun osColor(os: String?): Color = when {
    os?.contains("windows", true) == true -> Color(0xFF1689E8)
    os?.contains("linux", true) == true -> Color(0xFFF0A020)
    os?.contains("mac", true) == true -> Color(0xFF8793A1)
    else -> BrandBlue
}

private fun assetStatusColor(asset: AssetSummary): Color = when {
    asset.lifecycle.contains("wycof", true) || asset.lifecycle.contains("retir", true) -> DangerRed
    asset.lastSeen.isNullOrBlank() -> WarningAmber
    else -> SuccessGreen
}

@Composable private fun ModernAssetDetailScreen(
    data: AssetDetail,
    dictionaries: Map<String, List<DictionaryEntry>>,
    canWrite: Boolean,
    padding: PaddingValues,
    saving: Boolean,
    mutationVersion: Int,
    onSaveAssignment: (String, AssignmentWrite) -> Unit,
) {
    var editAssignment by remember(data.asset.id) { mutableStateOf(false) }
    var tab by rememberSaveable(data.asset.id) { mutableIntStateOf(0) }
    LaunchedEffect(mutationVersion) { editAssignment = false }
    if (editAssignment) {
        ModernAssignmentEditor(data.asset, dictionaries, padding, saving, { editAssignment = false }) {
            onSaveAssignment(data.asset.id, it)
        }
        return
    }
    LazyColumn(
        Modifier.fillMaxSize().padding(padding),
        contentPadding = PaddingValues(bottom = 24.dp),
        verticalArrangement = Arrangement.spacedBy(0.dp),
    ) {
        item {
            Column(Modifier.fillMaxWidth().background(BrandNavy).padding(horizontal = 16.dp, vertical = 12.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    StatusPill(if (data.asset.lastSeen.isNullOrBlank()) "Brak kontaktu" else "Online", assetStatusColor(data.asset))
                    Spacer(Modifier.weight(1f))
                    if (canWrite) TextButton(onClick = { editAssignment = true }) { Text("EDYTUJ", color = Color.White, fontWeight = FontWeight.Bold) }
                }
            }
        }
        item {
            ElevatedCmdbCard(Modifier.padding(16.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Box(Modifier.size(72.dp).clip(RoundedCornerShape(14.dp)).background(osColor(data.asset.osFamily).copy(alpha = .14f)), contentAlignment = Alignment.Center) {
                        Icon(Icons.Outlined.Computer, null, tint = osColor(data.asset.osFamily), modifier = Modifier.size(40.dp))
                    }
                    Spacer(Modifier.width(16.dp))
                    Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                        Text(data.asset.hostname, style = MaterialTheme.typography.headlineMedium)
                        Text(data.asset.osFamily ?: "Nieznany system", color = MaterialTheme.colorScheme.onSurfaceVariant)
                        Text(data.asset.primaryIp ?: "Brak adresu IP", fontWeight = FontWeight.Medium)
                    }
                }
            }
        }
        item {
            val tabs = listOf("Podsumowanie", "Sprzęt", "System", "Sieć")
            TabRow(selectedTabIndex = tab, containerColor = MaterialTheme.colorScheme.surface) {
                tabs.forEachIndexed { index, title -> Tab(selected = tab == index, onClick = { tab = index }, text = { Text(title, maxLines = 1, fontSize = 11.sp) }) }
            }
        }
        when (tab) {
            0 -> item { AssetOverview(data) }
            1 -> item { JsonSection("Sprzęt", data.facts) }
            2 -> item { JsonSection("System i dane agenta", data.currentReport.orEmpty()) }
            else -> item { JsonSection("Sieć i atrybuty", data.attributes) }
        }
    }
}

@Composable private fun AssetOverview(data: AssetDetail) {
    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            DetailMetric("System", data.asset.osFamily ?: "—", Icons.Outlined.Dns, BrandBlue, Modifier.weight(1f))
            DetailMetric("Typ", data.asset.type, Icons.Outlined.Computer, Color(0xFF8B6CE5), Modifier.weight(1f))
        }
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            DetailMetric("Opiekun", data.asset.owner?.value ?: "Brak", Icons.Outlined.Person, if (data.asset.owner == null) DangerRed else SuccessGreen, Modifier.weight(1f))
            DetailMetric("Lokalizacja", data.asset.location?.value ?: "Brak", Icons.Outlined.LocationOn, WarningAmber, Modifier.weight(1f))
        }
        SectionHeading("Informacje")
        ElevatedCmdbCard {
            InfoLine("Użytkownik", data.asset.user?.value)
            InfoLine("Rola", data.asset.roleLabel)
            InfoLine("Miejsce", data.asset.place)
            InfoLine("Źródło", data.asset.source)
            InfoLine("Ostatni kontakt", data.asset.lastSeen)
        }
    }
}

@Composable private fun DetailMetric(label: String, value: String, icon: ImageVector, color: Color, modifier: Modifier) {
    ElevatedCmdbCard(modifier) {
        Icon(icon, null, tint = color)
        Spacer(Modifier.height(10.dp))
        Text(value, style = MaterialTheme.typography.titleMedium, maxLines = 2, overflow = TextOverflow.Ellipsis)
        Text(label, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable private fun JsonSection(title: String, values: Map<String, JsonElement>) {
    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        SectionHeading(title)
        if (values.isEmpty()) EmptyState("Brak danych w tej sekcji")
        values.forEach { (key, value) ->
            ElevatedCmdbCard { Text(humanize(key), style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.primary); Spacer(Modifier.height(5.dp)); Text(jsonDisplay(value), color = MaterialTheme.colorScheme.onSurfaceVariant) }
        }
    }
}

private fun JsonObject?.orEmpty(): Map<String, JsonElement> = this ?: emptyMap()
private fun humanize(value: String) = value.replace('_', ' ').replaceFirstChar { it.uppercase() }
private fun jsonDisplay(value: JsonElement): String = when (value) {
    JsonNull -> "—"
    is JsonPrimitive -> value.contentOrNull ?: value.toString()
    is JsonObject -> value.entries.take(5).joinToString("\n") { "${humanize(it.key)}: ${jsonDisplay(it.value)}" }
    else -> value.toString().take(600)
}

@Composable private fun ModernAssignmentEditor(
    asset: AssetSummary,
    dictionaries: Map<String, List<DictionaryEntry>>,
    padding: PaddingValues,
    saving: Boolean,
    onDismiss: () -> Unit,
    onSave: (AssignmentWrite) -> Unit,
) {
    var owner by remember { mutableStateOf(asset.owner?.id.orEmpty()) }
    var user by remember { mutableStateOf(asset.user?.id.orEmpty()) }
    var location by remember { mutableStateOf(asset.location?.id.orEmpty()) }
    var role by remember { mutableStateOf(asset.roleLabel.orEmpty()) }
    var place by remember { mutableStateOf(asset.place.orEmpty()) }
    val people = dictionaries["osoba"].orEmpty().map { it.id to it.value }
    val locations = dictionaries["lokalizacja"].orEmpty().map { it.id to it.value }
    BackHandler(enabled = !saving, onBack = onDismiss)
    LazyColumn(
        Modifier.fillMaxSize().padding(padding),
        contentPadding = PaddingValues(18.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        item { Text("Przypisania", style = MaterialTheme.typography.headlineMedium); Text(asset.hostname, color = MaterialTheme.colorScheme.onSurfaceVariant) }
        item { ModernChoiceField("Opiekun", owner, people) { owner = it } }
        item { ModernChoiceField("Użytkownik", user, people) { user = it } }
        item { ModernChoiceField("Lokalizacja", location, locations) { location = it } }
        item { OutlinedTextField(role, { role = it }, label = { Text("Rola") }, leadingIcon = { Icon(Icons.Outlined.Badge, null) }, modifier = Modifier.fillMaxWidth(), shape = RoundedCornerShape(12.dp)) }
        item { OutlinedTextField(place, { place = it }, label = { Text("Miejsce") }, leadingIcon = { Icon(Icons.Outlined.LocationOn, null) }, modifier = Modifier.fillMaxWidth(), shape = RoundedCornerShape(12.dp)) }
        item {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedButton(onClick = onDismiss, enabled = !saving, modifier = Modifier.weight(1f)) { Text("ANULUJ") }
                Button(
                    onClick = { onSave(AssignmentWrite(owner.ifBlank { null }, user.ifBlank { null }, location.ifBlank { null }, role.ifBlank { null }, place.ifBlank { null })) },
                    enabled = !saving,
                    modifier = Modifier.weight(1f),
                ) { Icon(Icons.Outlined.Save, null); Spacer(Modifier.width(6.dp)); Text("ZAPISZ") }
            }
        }
    }
}

@Composable internal fun ModernChoiceField(label: String, value: String, options: List<Pair<String, String>>, onChange: (String) -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    val shown = options.firstOrNull { it.first == value }?.second ?: "Wybierz"
    Box(Modifier.fillMaxWidth()) {
        OutlinedButton(onClick = { expanded = true }, modifier = Modifier.fillMaxWidth().height(56.dp), shape = RoundedCornerShape(12.dp)) {
            Text(label, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.weight(1f))
            Text(shown, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Icon(Icons.Outlined.KeyboardArrowDown, null)
        }
        DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            DropdownMenuItem(text = { Text("— brak —") }, onClick = { onChange(""); expanded = false })
            options.forEach { option -> DropdownMenuItem(text = { Text(option.second) }, onClick = { onChange(option.first); expanded = false }) }
        }
    }
}

@Composable private fun ModernChangesScreen(data: List<ChangeEntry>, padding: PaddingValues) {
    LazyColumn(
        Modifier.fillMaxSize().padding(padding),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(0.dp),
    ) {
        item { Text("Historia zmian", style = MaterialTheme.typography.headlineMedium); Spacer(Modifier.height(14.dp)) }
        itemsIndexed(data, key = { _, item -> item.id }) { index, change -> TimelineChange(change, index != data.lastIndex) }
        if (data.isEmpty()) item { EmptyState("Brak zarejestrowanych zmian") }
    }
}

@Composable private fun TimelineChange(change: ChangeEntry, showLine: Boolean) {
    val color = when (change.action.lowercase()) {
        "created", "utworzono", "added" -> SuccessGreen
        "deleted", "usunięto" -> DangerRed
        else -> BrandBlue
    }
    Row(Modifier.fillMaxWidth()) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Box(Modifier.size(34.dp).clip(CircleShape).background(color.copy(alpha = .14f)), contentAlignment = Alignment.Center) {
                Icon(Icons.Outlined.History, null, tint = color, modifier = Modifier.size(18.dp))
            }
            if (showLine) Canvas(Modifier.width(2.dp).height(94.dp)) { drawLine(color.copy(alpha = .28f), Offset(size.width / 2, 0f), Offset(size.width / 2, size.height), strokeWidth = size.width) }
        }
        Spacer(Modifier.width(12.dp))
        ElevatedCmdbCard(Modifier.weight(1f).padding(bottom = 12.dp)) {
            Text(change.hostname, style = MaterialTheme.typography.titleMedium)
            Text("${change.action}: ${change.label}", color = MaterialTheme.colorScheme.onSurfaceVariant)
            if (!change.oldValue.isNullOrBlank() || !change.newValue.isNullOrBlank()) {
                Spacer(Modifier.height(8.dp))
                Text("${change.oldValue ?: "—"}  →  ${change.newValue ?: "—"}", color = color, fontWeight = FontWeight.Medium)
            }
            Spacer(Modifier.height(6.dp))
            Text(change.occurredAt, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable internal fun ElevatedCmdbCard(modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
    Card(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(15.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        elevation = CardDefaults.cardElevation(defaultElevation = 2.dp),
    ) { Column(Modifier.fillMaxWidth().padding(15.dp), content = content) }
}

@Composable internal fun RoundIcon(icon: ImageVector, color: Color) {
    Box(Modifier.size(42.dp).clip(CircleShape).background(color.copy(alpha = .14f)), contentAlignment = Alignment.Center) {
        Icon(icon, null, tint = color, modifier = Modifier.size(22.dp))
    }
}

@Composable internal fun SectionHeading(text: String) = Text(text, style = MaterialTheme.typography.titleLarge)

@Composable internal fun StatusPill(text: String, color: Color) {
    Row(Modifier.clip(RoundedCornerShape(20.dp)).background(color.copy(alpha = .17f)).padding(horizontal = 10.dp, vertical = 5.dp), verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(7.dp).clip(CircleShape).background(color)); Spacer(Modifier.width(6.dp)); Text(text, color = color, style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.Bold)
    }
}

@Composable internal fun InfoLine(label: String, value: String?) {
    if (!value.isNullOrBlank()) Row(Modifier.fillMaxWidth().padding(vertical = 7.dp), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(label, Modifier.weight(1f), color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(value, Modifier.weight(1.2f), fontWeight = FontWeight.Medium)
    }
}

@Composable internal fun EmptyState(text: String) {
    ElevatedCmdbCard { Text(text, Modifier.align(Alignment.CenterHorizontally), color = MaterialTheme.colorScheme.onSurfaceVariant) }
}
