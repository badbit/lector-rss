/// Lecturas y escrituras locales.
///
/// Toda escritura que deba viajar a otros dispositivos pasa por aquí, porque es
/// donde se anota en el diario: el reloj del campo y la cola de salida se
/// actualizan en la misma transacción que el dato, o acabarían desfasados.
library;

import 'dart:convert';

import 'package:sqflite/sqflite.dart';

import '../models.dart';
import 'database.dart';

class Repo {
  Repo(this.app);

  final AppDatabase app;
  Database get db => app.db;

  // ================================================================ lecturas
  Future<List<Folder>> carpetas() async {
    final filas = await db.query('folders',
        where: 'deleted = 0', orderBy: 'position, name');
    return filas
        .map((f) => Folder(
              id: f['id'] as String,
              parentId: f['parent_id'] as String?,
              name: f['name'] as String,
              position: f['position'] as int,
            ))
        .toList();
  }

  Future<List<Feed>> feeds() async {
    final filas = await db.rawQuery('''
      SELECT f.*, (
        SELECT COUNT(*) FROM entries e
        JOIN entry_state s ON s.entry_id = e.id
        WHERE e.feed_id = f.id AND s.read = 0
      ) AS unread
      FROM feeds f WHERE f.deleted = 0
      ORDER BY COALESCE(NULLIF(f.custom_title, ''), f.title), f.url
    ''');
    return filas
        .map((f) => Feed(
              id: f['id'] as String,
              folderId: f['folder_id'] as String?,
              url: f['url'] as String,
              siteUrl: f['site_url'] as String?,
              title: (f['title'] ?? '') as String,
              customTitle: f['custom_title'] as String?,
              iconUrl: f['icon_url'] as String?,
              sourceKind: (f['source_kind'] ?? 'feed') as String,
              unread: (f['unread'] ?? 0) as int,
            ))
        .toList();
  }

  Future<int> totalSinLeer() async {
    final filas = await db
        .rawQuery('SELECT COUNT(*) AS n FROM entry_state WHERE read = 0');
    return (filas.first['n'] ?? 0) as int;
  }

  /// Lista de artículos. `offset` permite paginar: la ventana del móvil puede
  /// tener decenas de miles de entradas y construirlas todas congelaría la UI.
  Future<List<Entry>> entradas({
    String? feedId,
    String? folderId,
    bool soloSinLeer = false,
    bool soloGuardados = false,
    String? busqueda,
    int limit = 100,
    int offset = 0,
  }) async {
    final where = <String>['1=1'];
    final args = <Object?>[];

    if (feedId != null) {
      where.add('e.feed_id = ?');
      args.add(feedId);
    }
    if (folderId != null) {
      where.add(
          'e.feed_id IN (SELECT id FROM feeds WHERE folder_id = ? AND deleted = 0)');
      args.add(folderId);
    }
    if (soloSinLeer) where.add('s.read = 0');
    if (soloGuardados) where.add('s.starred = 1');
    if (busqueda != null && busqueda.trim().isNotEmpty) {
      where.add('(e.title LIKE ? OR e.summary LIKE ?)');
      final patron = '%${busqueda.trim()}%';
      args
        ..add(patron)
        ..add(patron);
    }

    args
      ..add(limit)
      ..add(offset);
    final filas = await db.rawQuery('''
      SELECT e.*, s.read, s.starred FROM entries e
      JOIN entry_state s ON s.entry_id = e.id
      WHERE ${where.join(' AND ')}
      ORDER BY e.published_at DESC LIMIT ? OFFSET ?
    ''', args);

    return filas.map(_aEntrada).toList();
  }

  Future<Entry?> entrada(String id) async {
    final filas = await db.rawQuery('''
      SELECT e.*, s.read, s.starred, b.html AS body_html, b.text AS body_text
      FROM entries e
      JOIN entry_state s ON s.entry_id = e.id
      LEFT JOIN entry_bodies b ON b.entry_id = e.id
      WHERE e.id = ?
    ''', [id]);
    return filas.isEmpty ? null : _aEntrada(filas.first);
  }

  Entry _aEntrada(Map<String, Object?> f) => Entry(
        id: f['id'] as String,
        feedId: f['feed_id'] as String,
        url: f['url'] as String?,
        title: (f['title'] ?? '') as String,
        author: f['author'] as String?,
        summary: f['summary'] as String?,
        publishedAt: (f['published_at'] ?? 0) as int,
        read: (f['read'] ?? 0) == 1,
        starred: (f['starred'] ?? 0) == 1,
        bodyHtml: f['body_html'] as String?,
        bodyText: f['body_text'] as String?,
      );

  Future<void> guardarCuerpo(
      String entryId, String? html, String? texto) async {
    await db.insert(
      'entry_bodies',
      {
        'entry_id': entryId,
        'html': html,
        'text': texto,
        'fetched_at': DateTime.now().millisecondsSinceEpoch,
      },
      conflictAlgorithm: ConflictAlgorithm.replace,
    );
    await db.update('entries', {'has_body': 1},
        where: 'id = ?', whereArgs: [entryId]);
  }

  // ============================================== escrituras que se sincronizan
  Future<void> marcarLeido(List<String> ids, bool leido) =>
      _marcar(ids, 'read', leido);

  Future<void> marcarGuardado(List<String> ids, bool guardado) =>
      _marcar(ids, 'starred', guardado);

  Future<void> _marcar(List<String> ids, String campo, bool valor) async {
    if (ids.isEmpty) return;
    final deviceId = await app.deviceId();
    final ahora = DateTime.now().millisecondsSinceEpoch;
    final columnaFecha = campo == 'read' ? 'read_at' : 'star_at';

    for (final id in ids) {
      final lamport = await app.tickLamport();
      await db.transaction((txn) async {
        await txn.rawInsert('''
          INSERT INTO entry_state (entry_id, $campo, $columnaFecha) VALUES (?, ?, ?)
          ON CONFLICT(entry_id) DO UPDATE SET $campo = excluded.$campo,
            $columnaFecha = excluded.$columnaFecha
        ''', [id, valor ? 1 : 0, valor ? ahora : null]);

        await _anotar(
            txn, 'entry_state', id, campo, valor, lamport, deviceId, ahora);
      });
    }
  }

  Future<void> marcarFeedLeido(String feedId) async {
    final filas = await db.rawQuery('''
      SELECT s.entry_id FROM entry_state s
      JOIN entries e ON e.id = s.entry_id
      WHERE e.feed_id = ? AND s.read = 0
    ''', [feedId]);
    await marcarLeido(filas.map((f) => f['entry_id'] as String).toList(), true);
  }

  /// Anota el cambio en el reloj del campo y en la cola de salida.
  Future<void> _anotar(
    DatabaseExecutor txn,
    String entidad,
    String entidadId,
    String campo,
    Object? valor,
    int lamport,
    String deviceId,
    int ts,
  ) async {
    await txn.rawInsert('''
      INSERT INTO field_clock (entity, entity_id, field, lamport, device_id)
      VALUES (?, ?, ?, ?, ?)
      ON CONFLICT(entity, entity_id, field) DO UPDATE SET
        lamport = excluded.lamport, device_id = excluded.device_id
    ''', [entidad, entidadId, campo, lamport, deviceId]);

    await txn.insert('outbox', {
      'device_id': deviceId,
      'lamport': lamport,
      'entity': entidad,
      'entity_id': entidadId,
      'field': campo,
      'value_json': jsonEncode(valor),
      'ts': ts,
    });
  }

  // =================================================================== outbox
  Future<List<(int, ChangeOp)>> pendientesDeSubir({int limite = 500}) async {
    final filas = await db.query('outbox', orderBy: 'id', limit: limite);
    return filas
        .map((f) => (
              f['id'] as int,
              ChangeOp(
                deviceId: f['device_id'] as String,
                lamport: f['lamport'] as int,
                entity: f['entity'] as String,
                entityId: f['entity_id'] as String,
                field: f['field'] as String,
                value: jsonDecode(f['value_json'] as String),
                ts: f['ts'] as int,
              )
            ))
        .toList();
  }

  Future<void> limpiarOutbox(List<int> ids) async {
    if (ids.isEmpty) return;
    final marcas = List.filled(ids.length, '?').join(',');
    await db.rawDelete('DELETE FROM outbox WHERE id IN ($marcas)', ids);
  }

  Future<int> cambiosPendientes() async {
    final filas = await db.rawQuery('SELECT COUNT(*) AS n FROM outbox');
    return (filas.first['n'] ?? 0) as int;
  }
}
