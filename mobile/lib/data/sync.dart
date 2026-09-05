/// Motor de sincronización del móvil.
///
/// Réplica en Dart del modelo del núcleo Python: diario de cambios con reloj de
/// Lamport y last-write-wins **por campo**, con el `device_id` como desempate.
/// De ahí salen las tres propiedades que hacen que los dispositivos converjan
/// sin coordinarse: aplicar el mismo lote dos veces no cambia nada, el orden de
/// llegada da igual, y ante el mismo conflicto los dos lados eligen lo mismo.
library;

import 'dart:convert';

import 'package:sqflite/sqflite.dart';

import '../api/hub_client.dart';
import '../models.dart';
import 'database.dart';
import 'repo.dart';

/// Columnas que cada entidad admite. Es una lista blanca: el nombre del campo
/// viene de la red y acaba interpolado en el SQL.
const _camposPorEntidad = <String, Set<String>>{
  'entry': {'data', 'deleted'},
  'entry_state': {'read', 'starred'},
  'entry_tag': {'deleted'},
  'tag': {'name', 'color', 'deleted'},
  'feed': {
    'url',
    'title',
    'custom_title',
    'folder_id',
    'disabled',
    'deleted',
    'source_kind',
  },
  'folder': {'name', 'parent_id', 'position', 'deleted'},
};

const _tablas = <String, String>{
  'entry_state': 'entry_state',
  'entry_tag': 'entry_tags',
  'tag': 'tags',
  'feed': 'feeds',
  'folder': 'folders',
};

const _booleanos = {'read', 'starred', 'deleted', 'disabled'};

class SyncEngine {
  SyncEngine({
    required this.app,
    required this.repo,
    required this.client,
    this.scope = const SyncScope(),
  });

  final AppDatabase app;
  final Repo repo;
  final HubClient client;
  final SyncScope scope;

  Database get db => app.db;

  // ==================================================================== ciclo
  Future<SyncStats> syncOnce({String nombre = 'móvil'}) async {
    final stats = SyncStats();
    final deviceId = await app.deviceId();

    try {
      await client.register(deviceId, nombre, scope);
    } catch (_) {
      // Que falle el registro no impide sincronizar: el hub crea el cliente al
      // recibir el primer push.
    }

    if (await app.cursor() == 0) {
      await _bootstrap(deviceId);
      stats.bootstrap = true;
    }

    stats.uploaded = await _push(deviceId);
    final bajada = await _pull(deviceId);
    stats
      ..downloaded = bajada.downloaded
      ..applied = bajada.applied
      ..ignored = bajada.ignored
      ..pending = bajada.pending;

    await replayPending();
    return stats;
  }

  // ================================================================ arranque
  /// Un dispositivo nuevo no puede reproducir un diario de millones de
  /// operaciones: el hub sirve una foto del ámbito y el cursor exacto.
  Future<void> _bootstrap(String deviceId) async {
    final foto = await client.snapshot(deviceId, days: scope.days);

    await db.transaction((txn) async {
      await _volcar(
          txn, 'folders', foto['folders'], (j) => Folder.fromJson(j).toRow());
      await _volcar(
          txn, 'feeds', foto['feeds'], (j) => Feed.fromJson(j).toRow());
      await _volcar(
          txn,
          'tags',
          foto['tags'],
          (j) => {
                'id': j['id'],
                'name': j['name'] ?? '',
                'color': j['color'],
                'deleted': _entero(j['deleted']),
              });
      await _volcar(
          txn,
          'entries',
          foto['entries'],
          (j) => {
                'id': j['id'],
                'feed_id': j['feed_id'],
                'url': j['url'],
                'title': j['title'] ?? '',
                'author': j['author'],
                'summary': j['summary'],
                'published_at': j['published_at'] ?? 0,
                'has_body': 0,
              });
      await _volcar(
          txn,
          'entry_state',
          foto['state'],
          (j) => {
                'entry_id': j['entry_id'],
                'read': _entero(j['read']),
                'starred': _entero(j['starred']),
                'read_at': j['read_at'],
                'star_at': j['star_at'],
              });
      await _volcar(
          txn,
          'entry_tags',
          foto['entry_tags'],
          (j) => {
                'entry_id': j['entry_id'],
                'tag_id': j['tag_id'],
                'deleted': _entero(j['deleted']),
              });
    });

    await app.setCursor((foto['cursor'] ?? 0) as int);
    if (foto['server_lamport'] != null) {
      await app.observeLamport(foto['server_lamport'] as int);
    }
  }

  Future<void> _volcar(
    DatabaseExecutor txn,
    String tabla,
    Object? filas,
    Map<String, Object?> Function(Map<String, dynamic>) mapear,
  ) async {
    if (filas is! List) return;
    final lote = txn.batch();
    for (final fila in filas) {
      lote.insert(tabla, mapear(fila as Map<String, dynamic>),
          conflictAlgorithm: ConflictAlgorithm.replace);
    }
    await lote.commit(noResult: true);
  }

  // ==================================================================== subida
  Future<int> _push(String deviceId) async {
    var total = 0;
    while (true) {
      final lote = await repo.pendientesDeSubir();
      if (lote.isEmpty) break;

      final respuesta =
          await client.push(deviceId, lote.map((e) => e.$2).toList());
      // Solo ahora, con el hub habiendo confirmado, se vacía la cola: si se
      // borrase antes, un corte de red perdería el cambio para siempre.
      await repo.limpiarOutbox(lote.map((e) => e.$1).toList());
      total += respuesta.accepted;
      if (lote.length < 500) break;
    }
    return total;
  }

  // ==================================================================== bajada
  Future<({int downloaded, int applied, int ignored, int pending})> _pull(
      String deviceId) async {
    var descargadas = 0, aplicadas = 0, descartadas = 0, aparcadas = 0;

    while (true) {
      final desde = await app.cursor();
      final respuesta = await client.pull(deviceId, desde);

      if (respuesta.ops.isNotEmpty) {
        final r = await applyOps(respuesta.ops);
        descargadas += respuesta.ops.length;
        aplicadas += r.applied;
        descartadas += r.ignored;
        aparcadas += r.pending;
      }
      await app.setCursor(respuesta.cursor);
      if (!respuesta.hasMore) break;
    }
    return (
      downloaded: descargadas,
      applied: aplicadas,
      ignored: descartadas,
      pending: aparcadas,
    );
  }

  // ============================================================== aplicación
  Future<({int applied, int ignored, int pending})> applyOps(
      List<ChangeOp> ops) async {
    var aplicadas = 0, descartadas = 0, aparcadas = 0;

    for (final op in ops) {
      await app.observeLamport(op.lamport);
      final resultado = await _aplicarUna(op);
      switch (resultado) {
        case 'applied':
          aplicadas++;
        case 'pending':
          aparcadas++;
          await _aparcar(op);
        default:
          descartadas++;
      }
    }
    return (applied: aplicadas, ignored: descartadas, pending: aparcadas);
  }

  Future<String> _aplicarUna(ChangeOp op) async {
    final campos = _camposPorEntidad[op.entity];
    if (campos == null || !campos.contains(op.field)) return 'ignored';

    // El reloj se guarda por campo: sin eso, «leído» y «guardado» compartirían
    // reloj y el orden de llegada decidiría cuál sobrevive.
    final reloj = await _reloj(op.entity, op.entityId, op.field);
    if (reloj != null && !op.winsOver(reloj.$1, reloj.$2)) return 'ignored';

    final valor = _booleanos.contains(op.field) ? _entero(op.value) : op.value;

    switch (op.entity) {
      case 'entry':
        if (op.field == 'deleted') {
          if (_entero(op.value) != 1) return 'ignored';
          await _eliminarEntrada(op.entityId);
          break;
        }
        final borrado = await _reloj('entry', op.entityId, 'deleted');
        if (borrado != null && !op.winsOver(borrado.$1, borrado.$2)) {
          return 'ignored';
        }
        if (op.value is! Map) return 'ignored';
        final datos = Map<String, dynamic>.from(op.value as Map);
        final feedId = datos['feed_id'];
        if (feedId is! String || !await _existe('feeds', 'id', feedId)) {
          return 'pending';
        }
        // Si es una edición, el cuerpo cacheado ya no corresponde a estos
        // metadatos. Se volverá a pedir al hub cuando se abra el artículo.
        await db.delete('entry_bodies',
            where: 'entry_id = ?', whereArgs: [op.entityId]);
        await db.rawInsert('''
          INSERT INTO entries (id, feed_id, url, title, author, summary, published_at, has_body)
          VALUES (?, ?, ?, ?, ?, ?, ?, 0)
          ON CONFLICT(id) DO UPDATE SET feed_id = excluded.feed_id,
            url = excluded.url, title = excluded.title, author = excluded.author,
            summary = excluded.summary, published_at = excluded.published_at, has_body = 0
        ''', [
          op.entityId,
          feedId,
          datos['url'],
          datos['title'] ?? '',
          datos['author'],
          datos['summary'],
          datos['published_at'] ?? 0,
        ]);
        await db.rawInsert('''
          INSERT INTO entry_state (entry_id, read, starred) VALUES (?, 0, 0)
          ON CONFLICT(entry_id) DO NOTHING
        ''', [op.entityId]);

      case 'entry_state':
        // Puede llegar el estado de un artículo que este móvil aún no tiene.
        if (!await _existe('entries', 'id', op.entityId)) {
          if (await _reloj('entry', op.entityId, 'deleted') != null) {
            return 'ignored';
          }
          return 'pending';
        }
        final columnaFecha = op.field == 'read' ? 'read_at' : 'star_at';
        await db.rawInsert('''
          INSERT INTO entry_state (entry_id, ${op.field}, $columnaFecha)
          VALUES (?, ?, ?)
          ON CONFLICT(entry_id) DO UPDATE SET ${op.field} = excluded.${op.field},
            $columnaFecha = excluded.$columnaFecha
        ''', [op.entityId, valor, valor == 1 ? op.ts : null]);

      case 'entry_tag':
        final partes = op.entityId.split(':');
        if (partes.length != 2) return 'ignored';
        if (!await _existe('entries', 'id', partes[0])) {
          if (await _reloj('entry', partes[0], 'deleted') != null) {
            return 'ignored';
          }
          return 'pending';
        }
        if (!await _existe('tags', 'id', partes[1])) return 'pending';
        await db.rawInsert('''
          INSERT INTO entry_tags (entry_id, tag_id, deleted) VALUES (?, ?, ?)
          ON CONFLICT(entry_id, tag_id) DO UPDATE SET deleted = excluded.deleted
        ''', [partes[0], partes[1], valor]);

      default:
        final tabla = _tablas[op.entity]!;
        if (!await _existe(tabla, 'id', op.entityId)) {
          await _crearEsqueleto(tabla, op.entityId);
        }
        await db.rawUpdate(
          'UPDATE $tabla SET ${op.field} = ? WHERE id = ?',
          [valor, op.entityId],
        );
    }

    await _guardarReloj(op);
    return 'applied';
  }

  Future<void> _eliminarEntrada(String id) async {
    final lote = db.batch();
    lote.delete('entry_tags', where: 'entry_id = ?', whereArgs: [id]);
    lote.delete('entry_state', where: 'entry_id = ?', whereArgs: [id]);
    lote.delete('entry_bodies', where: 'entry_id = ?', whereArgs: [id]);
    lote.delete('entries', where: 'id = ?', whereArgs: [id]);
    lote.delete('sync_pending',
        where: 'entity_id = ? OR entity_id LIKE ?', whereArgs: [id, '$id:%']);
    lote.delete('outbox',
        where: 'entity_id = ? OR entity_id LIKE ?', whereArgs: [id, '$id:%']);
    lote.delete('field_clock',
        where: "(entity = 'entry_state' AND entity_id = ?) "
            "OR (entity = 'entry_tag' AND entity_id LIKE ?)",
        whereArgs: [id, '$id:%']);
    await lote.commit(noResult: true);
  }

  /// Las operaciones de una entidad llegan como campos sueltos y en cualquier
  /// orden: la primera que llega tiene que poder materializar la fila.
  Future<void> _crearEsqueleto(String tabla, String id) async {
    switch (tabla) {
      case 'feeds':
        await db.insert(
            'feeds', {'id': id, 'url': 'urn:pendiente:$id', 'title': ''});
      case 'folders':
        await db.insert('folders', {'id': id, 'name': ''});
      case 'tags':
        await db.insert('tags', {'id': id, 'name': ''});
    }
  }

  Future<(int, String)?> _reloj(
      String entidad, String entidadId, String campo) async {
    final filas = await db.query('field_clock',
        columns: ['lamport', 'device_id'],
        where: 'entity = ? AND entity_id = ? AND field = ?',
        whereArgs: [entidad, entidadId, campo]);
    if (filas.isEmpty) return null;
    return (filas.first['lamport'] as int, filas.first['device_id'] as String);
  }

  Future<void> _guardarReloj(ChangeOp op) async {
    await db.rawInsert('''
      INSERT INTO field_clock (entity, entity_id, field, lamport, device_id)
      VALUES (?, ?, ?, ?, ?)
      ON CONFLICT(entity, entity_id, field) DO UPDATE SET
        lamport = excluded.lamport, device_id = excluded.device_id
    ''', [op.entity, op.entityId, op.field, op.lamport, op.deviceId]);
  }

  Future<bool> _existe(String tabla, String columna, String valor) async {
    final filas = await db
        .rawQuery('SELECT 1 FROM $tabla WHERE $columna = ? LIMIT 1', [valor]);
    return filas.isNotEmpty;
  }

  // ==================================================== operaciones huérfanas
  Future<void> _aparcar(ChangeOp op) async {
    await db.insert(
      'sync_pending',
      {
        'entity': op.entity,
        'entity_id': op.entityId,
        'field': op.field,
        'value_json': jsonEncode(op.value),
        'lamport': op.lamport,
        'device_id': op.deviceId,
        'ts': op.ts,
      },
      conflictAlgorithm: ConflictAlgorithm.ignore,
    );
  }

  /// Reintenta las operaciones que llegaron antes que su artículo.
  ///
  /// Descartarlas perdería el cambio para siempre: es lo que pasa cuando el PC
  /// marca como leído algo que el móvil todavía no ha descargado.
  Future<int> replayPending() async {
    final filas = await db.query('sync_pending', orderBy: 'lamport');
    if (filas.isEmpty) return 0;

    var recuperadas = 0;
    for (final fila in filas) {
      final op = ChangeOp(
        deviceId: fila['device_id'] as String,
        lamport: fila['lamport'] as int,
        entity: fila['entity'] as String,
        entityId: fila['entity_id'] as String,
        field: fila['field'] as String,
        value: jsonDecode(fila['value_json'] as String),
        ts: fila['ts'] as int,
      );
      final resultado = await _aplicarUna(op);
      if (resultado == 'pending') continue; // su artículo sigue sin llegar
      if (resultado == 'applied') recuperadas++;
      await db.delete('sync_pending', where: 'id = ?', whereArgs: [fila['id']]);
    }
    return recuperadas;
  }
}

int _entero(Object? v) => (v == true || v == 1) ? 1 : 0;
