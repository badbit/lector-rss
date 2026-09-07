import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';
import 'package:rssmovil/api/hub_client.dart';
import 'package:rssmovil/data/database.dart';
import 'package:rssmovil/data/repo.dart';
import 'package:rssmovil/data/sync.dart';
import 'package:rssmovil/models.dart';

Map<String, dynamic> op(String entity, String id, String field, Object? value,
        {int clock = 20}) =>
    {
      'entity': entity,
      'entity_id': id,
      'field': field,
      'value': value,
      'lamport': clock,
      'device_id': 'hub',
      'ts': 1000,
    };

Map<String, dynamic> entry(String id) => {
      'id': id,
      'feed_id': 'F1',
      'title': 'Artículo $id',
      'published_at': 1000,
    };

Map<String, dynamic> snapshot() => {
      'version': 1,
      'cursor': 10,
      'entries_cursor': 1,
      'server_lamport': 10,
      'feeds': [
        {'id': 'F1', 'url': 'https://example.test/rss', 'title': 'Feed'}
      ],
      'entries': [entry('E1')],
      'state': [
        {'entry_id': 'E1', 'read': 1, 'starred': 0}
      ],
      'field_clocks': [
        {
          'entity': 'entry_state',
          'entity_id': 'E1',
          'field': 'read',
          'lamport': 10,
          'device_id': 'hub',
        }
      ],
    };

void main() {
  late Directory temp;
  late AppDatabase app;
  late Repo repo;
  late HubClient client;
  late SyncEngine sync;
  late Future<Map<String, dynamic>> Function(Uri) delta;

  setUpAll(() {
    sqfliteFfiInit();
    databaseFactory = databaseFactoryFfi;
  });
  setUp(() async {
    temp = Directory.systemTemp.createTempSync('rss-delta-');
    app = await AppDatabase.open(ruta: '${temp.path}/mobile.db');
    repo = Repo(app);
    delta = (_) async => {'ops': [], 'cursor': 10, 'entries_cursor': 1};
    client = HubClient(
        baseUrl: 'http://hub',
        token: '',
        cliente: MockClient((r) async {
          final Object data;
          switch (r.url.path) {
            case '/sync/register':
              data = {'ok': true};
            case '/sync/snapshot':
              data = snapshot();
            case '/sync/pull':
              data = await delta(r.url);
            case '/sync/push':
              data = {'accepted': (jsonDecode(r.body)['ops'] as List).length};
            default:
              throw StateError('Petición inesperada: ${r.url}');
          }
          return http.Response(jsonEncode(data), 200,
              headers: {'content-type': 'application/json; charset=utf-8'});
        }));
    sync = SyncEngine(app: app, repo: repo, client: client);
  });
  tearDown(() async {
    client.close();
    await app.db.close();
    temp.deleteSync(recursive: true);
  });

  test('el arranque importa los relojes y rechaza una marca atrasada',
      () async {
    await sync.syncOnce();
    final result = await sync.applyOps([
      ChangeOp.fromJson(op('entry_state', 'E1', 'read', false, clock: 5)),
    ]);
    expect(result.ignored, 1);
    expect((await repo.entrada('E1'))!.read, isTrue);
    expect((await app.db.query('node')).first['entries_cursor'], 1);
  });

  test('la migración conserva identidad y cursor de la base anterior',
      () async {
    final path = '${temp.path}/version1.db';
    final old = await databaseFactory.openDatabase(path,
        options: OpenDatabaseOptions(
            version: 1,
            onCreate: (db, _) async {
              await db.execute('''CREATE TABLE node (id INTEGER PRIMARY KEY,
            device_id TEXT, lamport INTEGER, last_pull_seq INTEGER)''');
              await db.execute(
                  'CREATE TABLE feeds (id TEXT PRIMARY KEY, url TEXT)');
              await db.insert('node', {
                'id': 1,
                'device_id': 'original',
                'lamport': 42,
                'last_pull_seq': 30,
              });
              await db.insert(
                  'feeds', {'id': 'F1', 'url': 'https://example.test/rss'});
            }));
    await old.close();
    final upgraded = await AppDatabase.open(ruta: path);
    try {
      expect(await upgraded.deviceId(), 'original');
      expect(await upgraded.cursor(), 30);
      expect((await upgraded.db.query('node')).first['entries_cursor'], 0);
      expect((await upgraded.db.query('feeds')).first['disabled'], 0);
    } finally {
      await upgraded.db.close();
    }
  });

  test('descarga páginas de artículos aun con el diario vacío', () async {
    await sync.syncOnce();
    final cursors = <String?>[];
    delta = (uri) async {
      final cursor = uri.queryParameters['entries_since'];
      cursors.add(cursor);
      final id = cursor == '1' ? 'E2' : 'E3';
      return {
        'ops': [],
        'cursor': 10,
        'entries': [entry(id)],
        'dependencies': [op('feed', 'F1', 'disabled', false)],
        'entry_ops': [op('entry_state', id, 'read', true)],
        'entries_cursor': cursor == '1' ? 2 : 3,
        'entries_has_more': cursor == '1',
      };
    };
    await sync.syncOnce();
    expect(cursors, ['1', '2']);
    expect((await repo.entrada('E2'))!.read, isTrue);
    expect((await repo.entrada('E3'))!.read, isTrue);
    expect((await app.db.query('node')).first['entries_cursor'], 3);
  });

  test(
      'una página conserva marcas hechas durante la petición y cuerpos locales',
      () async {
    await sync.syncOnce();
    await repo.guardarCuerpo('E1', '<p>local</p>', 'local');
    delta = (_) async {
      await app.db.rawUpdate('UPDATE node SET lamport = 9999');
      await repo.marcarLeido(['E1'], false);
      return {
        'ops': [],
        'cursor': 20,
        'entries_cursor': 1,
        'entries': [entry('E1')],
        'entry_ops': [op('entry_state', 'E1', 'read', true)],
      };
    };
    await sync.syncOnce();
    expect((await repo.entrada('E1'))!.read, isFalse);
    expect((await repo.entrada('E1'))!.bodyText, 'local');
    expect(await repo.cambiosPendientes(), 1);
  });

  test(
      'un error al aplicar una página no avanza sus cursores ni deja artículos',
      () async {
    await sync.syncOnce();
    delta = (_) async => {
          'ops': [op('feed', 'F1', 'title', null)],
          'cursor': 20,
          'entries_cursor': 2,
          'entries': [entry('E2')],
        };
    await expectLater(sync.syncOnce(), throwsA(isA<DatabaseException>()));
    expect(await repo.entrada('E2'), isNull);
    expect(await app.cursor(), 10);
    expect((await app.db.query('node')).first['entries_cursor'], 1);
  });
}
