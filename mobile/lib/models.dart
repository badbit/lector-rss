/// Entidades compartidas con el núcleo Python.
///
/// Los nombres de los campos coinciden con los del JSON del hub a propósito:
/// cualquier divergencia aquí se convierte en un fallo de sincronización difícil
/// de encontrar.
library;

class Folder {
  Folder({
    required this.id,
    this.parentId,
    required this.name,
    this.position = 0,
    this.deleted = false,
  });

  final String id;
  final String? parentId;
  final String name;
  final int position;
  final bool deleted;

  factory Folder.fromJson(Map<String, dynamic> j) => Folder(
        id: j['id'] as String,
        parentId: j['parent_id'] as String?,
        name: (j['name'] ?? '') as String,
        position: (j['position'] ?? 0) as int,
        deleted: _bool(j['deleted']),
      );

  Map<String, Object?> toRow() => {
        'id': id,
        'parent_id': parentId,
        'name': name,
        'position': position,
        'deleted': deleted ? 1 : 0,
      };
}

class Feed {
  Feed({
    required this.id,
    this.folderId,
    required this.url,
    this.siteUrl,
    this.title = '',
    this.customTitle,
    this.iconUrl,
    this.sourceKind = 'feed',
    this.deleted = false,
    this.unread = 0,
  });

  final String id;
  final String? folderId;
  final String url;
  final String? siteUrl;
  final String title;
  final String? customTitle;
  final String? iconUrl;
  final String sourceKind;
  final bool deleted;
  final int unread;

  String get displayTitle => (customTitle?.isNotEmpty ?? false)
      ? customTitle!
      : (title.isNotEmpty ? title : url);

  /// `true` si no viene de un RSS, sino de raspar o vigilar una web.
  bool get isScraped => sourceKind != 'feed';

  factory Feed.fromJson(Map<String, dynamic> j) => Feed(
        id: j['id'] as String,
        folderId: j['folder_id'] as String?,
        url: (j['url'] ?? '') as String,
        siteUrl: j['site_url'] as String?,
        title: (j['title'] ?? '') as String,
        customTitle: j['custom_title'] as String?,
        iconUrl: j['icon_url'] as String?,
        sourceKind: (j['source_kind'] ?? 'feed') as String,
        deleted: _bool(j['deleted']),
        unread: (j['unread'] ?? 0) as int,
      );

  Map<String, Object?> toRow() => {
        'id': id,
        'folder_id': folderId,
        'url': url,
        'site_url': siteUrl,
        'title': title,
        'custom_title': customTitle,
        'icon_url': iconUrl,
        'source_kind': sourceKind,
        'deleted': deleted ? 1 : 0,
      };
}

class Entry {
  Entry({
    required this.id,
    required this.feedId,
    this.url,
    this.title = '',
    this.author,
    this.summary,
    required this.publishedAt,
    this.read = false,
    this.starred = false,
    this.bodyHtml,
    this.bodyText,
    this.tags = const [],
  });

  final String id;
  final String feedId;
  final String? url;
  final String title;
  final String? author;
  final String? summary;
  final int publishedAt;
  final bool read;
  final bool starred;
  final String? bodyHtml;
  final String? bodyText;
  final List<String> tags;

  bool get hasBody =>
      (bodyHtml?.isNotEmpty ?? false) || (bodyText?.isNotEmpty ?? false);

  DateTime get published => DateTime.fromMillisecondsSinceEpoch(publishedAt);

  factory Entry.fromJson(Map<String, dynamic> j) => Entry(
        id: j['id'] as String,
        feedId: j['feed_id'] as String,
        url: j['url'] as String?,
        title: (j['title'] ?? '') as String,
        author: j['author'] as String?,
        summary: j['summary'] as String?,
        publishedAt: (j['published_at'] ?? 0) as int,
        read: _bool(j['read']),
        starred: _bool(j['starred']),
        bodyHtml: j['body_html'] as String?,
        bodyText: j['body_text'] as String?,
        tags: ((j['tags'] ?? const []) as List).cast<String>(),
      );

  Entry copyWith(
          {bool? read, bool? starred, String? bodyHtml, String? bodyText}) =>
      Entry(
        id: id,
        feedId: feedId,
        url: url,
        title: title,
        author: author,
        summary: summary,
        publishedAt: publishedAt,
        read: read ?? this.read,
        starred: starred ?? this.starred,
        bodyHtml: bodyHtml ?? this.bodyHtml,
        bodyText: bodyText ?? this.bodyText,
        tags: tags,
      );
}

/// Una operación del diario de cambios: un campo de una entidad.
///
/// Es la unidad de sincronización. El conflicto se resuelve comparando
/// `(lamport, deviceId)`, con el mismo criterio que el hub, para que los dos
/// lados elijan siempre lo mismo sin coordinarse.
class ChangeOp {
  ChangeOp({
    required this.deviceId,
    required this.lamport,
    required this.entity,
    required this.entityId,
    required this.field,
    required this.value,
    required this.ts,
    this.seq,
  });

  final String deviceId;
  final int lamport;
  final String entity;
  final String entityId;
  final String field;
  final Object? value;
  final int ts;
  final int? seq;

  /// Gana si su reloj es mayor; a igualdad, decide el `deviceId`.
  bool winsOver(int otherLamport, String otherDevice) {
    if (lamport != otherLamport) return lamport > otherLamport;
    return deviceId.compareTo(otherDevice) > 0;
  }

  factory ChangeOp.fromJson(Map<String, dynamic> j) => ChangeOp(
        deviceId: j['device_id'] as String,
        lamport: j['lamport'] as int,
        entity: j['entity'] as String,
        entityId: j['entity_id'] as String,
        field: j['field'] as String,
        value: j['value'],
        ts: (j['ts'] ?? 0) as int,
        seq: j['seq'] as int?,
      );

  Map<String, Object?> toJson() => {
        'device_id': deviceId,
        'lamport': lamport,
        'entity': entity,
        'entity_id': entityId,
        'field': field,
        'value': value,
        'ts': ts,
      };
}

/// Qué parte del archivo replica este dispositivo.
///
/// El archivo del hub es permanente y puede tener millones de entradas; el
/// móvil declara una ventana y el hub filtra el delta en el servidor.
class SyncScope {
  const SyncScope({
    this.days = 30,
    this.includeStarred = true,
    this.includeUnread = true,
    this.maxEntries = 20000,
  });

  final int? days;
  final bool includeStarred;
  final bool includeUnread;
  final int maxEntries;

  Map<String, Object?> toJson() => {
        'days': days,
        'include_starred': includeStarred,
        'include_unread': includeUnread,
        'folder_ids': const <String>[],
        'feed_ids': const <String>[],
        'max_entries': maxEntries,
      };
}

class SyncStats {
  SyncStats({
    this.uploaded = 0,
    this.downloaded = 0,
    this.applied = 0,
    this.ignored = 0,
    this.pending = 0,
    this.bootstrap = false,
  });

  int uploaded;
  int downloaded;
  int applied;
  int ignored;
  int pending;
  bool bootstrap;

  @override
  String toString() {
    final base = '↑$uploaded ↓$downloaded · $applied aplicadas';
    return bootstrap ? 'arranque inicial · $base' : base;
  }
}

bool _bool(Object? v) => v == true || v == 1;
