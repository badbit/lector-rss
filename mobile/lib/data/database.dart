/// Espejo local en SQLite.
///
/// El esquema es una réplica parcial del hub: solo las tablas que el móvil
/// necesita, y los cuerpos de los artículos sin comprimir (el archivo completo
/// vive en el hub; aquí solo cabe una ventana).
///
/// Se usa SQL directo, igual que en el núcleo Python: el modelo de
/// sincronización depende de un reloj por campo y de una cola de salida, y un
/// ORM con generación de código no aportaría nada y añadiría piezas móviles.
library;

import 'dart:math';

import 'package:path/path.dart' as p;
import 'package:sqflite/sqflite.dart';

class AppDatabase {
  AppDatabase._(this.db);

  final Database db;

  /// `ruta` solo se pasa en las pruebas, que abren la base fuera de Android.
  static Future<AppDatabase> open(
      {String nombre = 'rss.db', String? ruta}) async {
    ruta ??= p.join(await getDatabasesPath(), nombre);
    final db = await openDatabase(
      ruta,
      version: 1,
      onConfigure: (d) async => d.execute('PRAGMA foreign_keys = ON'),
      onCreate: _crear,
    );
    final instancia = AppDatabase._(db);
    await instancia._asegurarNodo();
    return instancia;
  }

  static Future<void> _crear(Database db, int version) async {
    final sentencias = <String>[
      '''CREATE TABLE folders (
           id TEXT PRIMARY KEY, parent_id TEXT, name TEXT NOT NULL,
           position INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0)''',
      '''CREATE TABLE feeds (
           id TEXT PRIMARY KEY, folder_id TEXT, url TEXT NOT NULL, site_url TEXT,
           title TEXT NOT NULL DEFAULT '', custom_title TEXT, icon_url TEXT,
           source_kind TEXT NOT NULL DEFAULT 'feed', deleted INTEGER NOT NULL DEFAULT 0)''',
      '''CREATE TABLE entries (
           id TEXT PRIMARY KEY, feed_id TEXT NOT NULL, url TEXT,
           title TEXT NOT NULL DEFAULT '', author TEXT, summary TEXT,
           published_at INTEGER NOT NULL, has_body INTEGER NOT NULL DEFAULT 0)''',
      'CREATE INDEX idx_entries_feed_pub ON entries(feed_id, published_at DESC)',
      'CREATE INDEX idx_entries_pub ON entries(published_at DESC)',
      // Los cuerpos se piden al hub bajo demanda y se guardan para leer sin red.
      '''CREATE TABLE entry_bodies (
           entry_id TEXT PRIMARY KEY, html TEXT, text TEXT, fetched_at INTEGER)''',
      '''CREATE TABLE entry_state (
           entry_id TEXT PRIMARY KEY, read INTEGER NOT NULL DEFAULT 0,
           starred INTEGER NOT NULL DEFAULT 0, read_at INTEGER, star_at INTEGER)''',
      // Índice parcial: contar «no leídos» no debe recorrer toda la ventana.
      'CREATE INDEX idx_state_unread ON entry_state(entry_id) WHERE read = 0',
      'CREATE INDEX idx_state_starred ON entry_state(entry_id) WHERE starred = 1',
      '''CREATE TABLE tags (
           id TEXT PRIMARY KEY, name TEXT NOT NULL, color TEXT,
           deleted INTEGER NOT NULL DEFAULT 0)''',
      '''CREATE TABLE entry_tags (
           entry_id TEXT NOT NULL, tag_id TEXT NOT NULL,
           deleted INTEGER NOT NULL DEFAULT 0,
           PRIMARY KEY (entry_id, tag_id))''',
      // Reloj POR CAMPO, no por fila: es lo que hace que dos campos de la misma
      // entidad no se pisen según el orden en que lleguen las operaciones.
      '''CREATE TABLE field_clock (
           entity TEXT NOT NULL, entity_id TEXT NOT NULL, field TEXT NOT NULL,
           lamport INTEGER NOT NULL, device_id TEXT NOT NULL,
           PRIMARY KEY (entity, entity_id, field))''',
      // Cambios locales pendientes de subir. No se vacía hasta que el hub
      // confirma: un viaje sin cobertura no puede perder un «marcar leído».
      '''CREATE TABLE outbox (
           id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL,
           lamport INTEGER NOT NULL, entity TEXT NOT NULL, entity_id TEXT NOT NULL,
           field TEXT NOT NULL, value_json TEXT NOT NULL, ts INTEGER NOT NULL)''',
      // Operaciones que llegan antes que su artículo: se aparcan y se reintentan.
      '''CREATE TABLE sync_pending (
           id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL,
           entity_id TEXT NOT NULL, field TEXT NOT NULL, value_json TEXT NOT NULL,
           lamport INTEGER NOT NULL, device_id TEXT NOT NULL, ts INTEGER NOT NULL,
           UNIQUE (entity, entity_id, field, device_id, lamport))''',
      '''CREATE TABLE node (
           id INTEGER PRIMARY KEY CHECK (id = 1), device_id TEXT NOT NULL,
           lamport INTEGER NOT NULL DEFAULT 0, last_pull_seq INTEGER NOT NULL DEFAULT 0)''',
    ];
    for (final sentencia in sentencias) {
      await db.execute(sentencia);
    }
  }

  // ------------------------------------------------------------ identidad
  Future<void> _asegurarNodo() async {
    final filas = await db.query('node', where: 'id = 1');
    if (filas.isEmpty) {
      await db.insert('node', {
        'id': 1,
        'device_id': _nuevoId(),
        'lamport': 0,
        'last_pull_seq': 0,
      });
    }
  }

  Future<String> deviceId() async {
    final filas =
        await db.query('node', columns: ['device_id'], where: 'id = 1');
    return filas.first['device_id'] as String;
  }

  Future<int> cursor() async {
    final filas =
        await db.query('node', columns: ['last_pull_seq'], where: 'id = 1');
    return filas.first['last_pull_seq'] as int;
  }

  Future<void> setCursor(int valor) async {
    await db.rawUpdate(
      'UPDATE node SET last_pull_seq = MAX(last_pull_seq, ?) WHERE id = 1',
      [valor],
    );
  }

  /// Avanza el reloj lógico para una escritura local.
  Future<int> tickLamport() async {
    await db.rawUpdate('UPDATE node SET lamport = lamport + 1 WHERE id = 1');
    final filas = await db.query('node', columns: ['lamport'], where: 'id = 1');
    return filas.first['lamport'] as int;
  }

  /// Incorpora el reloj de otro dispositivo: max(local, remoto) + 1.
  Future<void> observeLamport(int remoto) async {
    await db.rawUpdate(
      'UPDATE node SET lamport = MAX(lamport, ?) + 1 WHERE id = 1',
      [remoto],
    );
  }

  Future<void> vaciar() async {
    await db.transaction((txn) async {
      for (final tabla in [
        'entry_tags',
        'entry_state',
        'entry_bodies',
        'entries',
        'feeds',
        'folders',
        'tags',
        'field_clock',
        'outbox',
        'sync_pending',
      ]) {
        await txn.delete(tabla);
      }
      await txn.update('node', {'last_pull_seq': 0}, where: 'id = 1');
    });
  }
}

/// ULID: 26 caracteres en base32 de Crockford, ordenable por tiempo.
///
/// Mismo formato que en el núcleo Python porque los identificadores se generan
/// en ambos lados estando desconectados y no pueden colisionar.
String _nuevoId() => nuevoUlid();

const _b32 = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
final _rand = Random.secure();

String nuevoUlid() {
  final ts = DateTime.now().millisecondsSinceEpoch;
  final buffer = StringBuffer();
  var resto = ts;
  final tiempo = List<String>.filled(10, '0');
  for (var i = 9; i >= 0; i--) {
    tiempo[i] = _b32[resto & 0x1F];
    resto >>= 5;
  }
  buffer.write(tiempo.join());
  for (var i = 0; i < 16; i++) {
    buffer.write(_b32[_rand.nextInt(32)]);
  }
  return buffer.toString();
}
