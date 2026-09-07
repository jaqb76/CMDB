package pl.hubzso.cmdb.data

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable data class LoginRequest(val email: String, val password: String)
@Serializable data class LoginResponse(
    @SerialName("access_token") val accessToken: String,
    @SerialName("expires_in") val expiresIn: Long,
    val user: User,
)

@Serializable data class User(
    val id: String,
    val email: String,
    @SerialName("full_name") val fullName: String? = null,
    val role: String,
    @SerialName("can_write") val canWrite: Boolean,
    val tenant: Tenant? = null,
)

@Serializable data class Tenant(val id: String, val name: String, val slug: String)

@Serializable data class Dashboard(
    val total: Int,
    val stale: Int,
    val unassigned: Int,
    @SerialName("with_agent") val withAgent: Int,
    @SerialName("by_os") val byOs: List<CountItem> = emptyList(),
    @SerialName("by_type") val byType: List<CountItem> = emptyList(),
    val recent: List<AssetSummary> = emptyList(),
    val changed: List<AssetSummary> = emptyList(),
)

@Serializable data class CountItem(val key: String? = null, val label: String, val count: Int)

@Serializable data class AssetSummary(
    val id: String,
    val hostname: String,
    val type: String,
    val source: String,
    @SerialName("os_family") val osFamily: String? = null,
    @SerialName("primary_ip") val primaryIp: String? = null,
    @SerialName("last_seen") val lastSeen: String? = null,
    val lifecycle: String,
    val owner: DictionaryEntry? = null,
    val user: DictionaryEntry? = null,
    val location: DictionaryEntry? = null,
    @SerialName("role_label") val roleLabel: String? = null,
    val place: String? = null,
)

@Serializable data class AssetPage(
    val items: List<AssetSummary>,
    val page: Int,
    @SerialName("page_size") val pageSize: Int,
    val total: Int,
)

@Serializable data class AssetDetail(
    val asset: AssetSummary,
    val facts: Map<String, kotlinx.serialization.json.JsonElement> = emptyMap(),
    val attributes: Map<String, kotlinx.serialization.json.JsonElement> = emptyMap(),
    @SerialName("current_report") val currentReport: kotlinx.serialization.json.JsonObject? = null,
)

@Serializable data class DictionaryEntry(
    val id: String,
    val category: String? = null,
    val value: String,
    val attributes: Map<String, kotlinx.serialization.json.JsonElement> = emptyMap(),
)

@Serializable data class DictionaryWrite(
    val attributes: Map<String, kotlinx.serialization.json.JsonElement>,
)

@Serializable data class DictionaryCategory(
    val key: String,
    val label: String,
)

@Serializable data class DictionaryField(
    val key: String,
    val label: String,
    val type: String,
    val required: Boolean = false,
    val group: String = "Pozostałe",
    val hint: String? = null,
    val options: List<String> = emptyList(),
    val target: String? = null,
    val format: String? = null,
    val min: Double? = null,
    val max: Double? = null,
)

@Serializable data class DictionarySchema(
    val category: String,
    val label: String,
    val version: Int,
    val fields: List<DictionaryField> = emptyList(),
)

@Serializable data class AssignmentWrite(
    @SerialName("owner_id") val ownerId: String? = null,
    @SerialName("user_id") val userId: String? = null,
    @SerialName("location_id") val locationId: String? = null,
    @SerialName("role_label") val roleLabel: String? = null,
    val place: String? = null,
)

@Serializable data class ChangeEntry(
    val id: String,
    @SerialName("asset_id") val assetId: String,
    val hostname: String,
    @SerialName("occurred_at") val occurredAt: String,
    val category: String,
    val action: String,
    val path: String,
    val label: String,
    @SerialName("old_value") val oldValue: String? = null,
    @SerialName("new_value") val newValue: String? = null,
)

@Serializable data class ReportDefinition(
    val id: String,
    val name: String,
    val type: String,
    val frequency: String,
    val recipients: String,
    val active: Boolean,
    @SerialName("last_status") val lastStatus: String? = null,
    @SerialName("last_sent_at") val lastSentAt: String? = null,
    @SerialName("send_to_vendors") val sendToVendors: Boolean = false,
    val columns: List<String> = emptyList(),
)

@Serializable data class ReportOption(val key: String, val label: String)
@Serializable data class ReportColumn(
    val key: String,
    val label: String,
    val group: String,
    val default: Boolean = false,
)
@Serializable data class ReportCatalog(
    val types: List<ReportOption>,
    val frequencies: List<ReportOption>,
    val columns: List<ReportColumn>,
)
@Serializable data class ReportWrite(
    val name: String,
    val type: String,
    val frequency: String,
    val recipients: String,
    val active: Boolean,
    @SerialName("send_to_vendors") val sendToVendors: Boolean,
    val columns: List<String>,
)

@Serializable data class ApiMessage(val detail: String)
