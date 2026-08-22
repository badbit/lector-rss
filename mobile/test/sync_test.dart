/// Pruebas del motor de sincronización del móvil.
///
/// Sin emulador: `sqflite_common_ffi` abre la misma base SQLite en el
/// escritorio. Lo que se comprueba aquí es lo que hace que dos dispositivos
/// converjan sin hablar entre ellos: aplicar dos veces no cambia nada, el
/// orden de llegada da igual, y ante el mismo conflicto ambos lados eligen lo
/// mismo.
library;

import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

import 'package:rssmovil/api/hub_client.dart';
import 'package:rssmovil/data/database.dart';
import 'package:rssmovil/data/repo.dart';
import 'package:rssmovil/data/sync.dart';
import 'package:rssmovil/models.dart';

/// El motor solo usa el cliente para hablar por la red; estas pruebas aplican
/// operaciones directamente, así que basta con una instancia que nunca se usa.
HubClient _clienteInerte() =>
    HubClient(baseUrl: 'http://invalido.invalido', token: '');

/// Un fichero por prueba. `inMemoryDatabasePath` no vale: sqflite cachea la
/// instancia por ruta y todas las pruebas acabarían compartiendo una base.
late Directory _tmp;
var _n = 0;

Future<(AppDatabase, Repo, SyncEngine)> _abrir() async {
  final app = await AppDatabase.open(ruta: '${_tmp.path}/prueba${_n++}.db');
  final repo = Repo(app);
  return (app, repo, SyncEngine(app: app, repo: repo, client: _clienteInerte()));
}

/// Un artículo con su feed, que es lo mínimo para que `entry_state` se aplique.
Future<void> _sembrar(AppDatabase app, {String entryId = 'E1'}) async {
  await app.db.insert('feeds', {'id': 'F1', 'url': 'https://ejemplo/feed', 'title': 'Feed'});
  await app.db.insert('entries', {
    'id': entryId,
    'feed_id': 'F1',
    'url': 'https://ejemplo/1',
    'title': 'Artículo',
    'published_at': 1000,
  });
}

ChangeOp _op(
  String entity,
  String entityId,
  String field,
  Object? value, {
  required int lamport,
  required String device,
}) =>
    ChangeOp(
      deviceId: device,
      lamport: lamport,
      entity: entity,
      entityId: entityId,
      field: field,
      value: value,
      ts: 1700000000000,
    );

Future<Map<String, Object?>?> _estado(AppDatabase app, String id) async {
  final filas = await app.db.query('entry_state', where: 'entry_id = ?', whereArgs: [id]);
  return filas.isEmpty ? null : filas.first;
}

void main() {
  setUpAll(() {
    sqfliteFfiInit();
    databaseFactory = databaseFactoryFfi;
    _tmp = Directory.systemTemp.createTempSync('rssmovil_test');
  });

  tearDownAll(() => _tmp.deleteSync(recursive: true));

  group('aplicación de operaciones', () {
    test('aplicar el mismo lote dos veces deja el mismo resultado', () async {
      final (app, _, sync) = await _abrir();
      await _sembrar(app);
      final ops = [_op('entry_state', 'E1', 'read', true, lamport: 5, device: 'B')];

      final primera = await sync.applyOps(ops);
      final segunda = await sync.applyOps(ops);

      expect(primera.applied, 1);
      // La segunda no gana al reloj que ella misma dejó: se descarta.
      expect(segunda.applied, 0);
      expect(segunda.ignored, 1);
      expect((await _estado(app, 'E1'))!['read'], 1);
    });

    test('«leído» y «guardado» no se pisan, llegue antes el que llegue', () async {
      // Dos campos de la misma fila con el MISMO reloj. Con un reloj por fila
      // el segundo se descartaría; con reloj por campo sobreviven los dos.
      for (final invertido in [false, true]) {
        final (app, _, sync) = await _abrir();
        await _sembrar(app);
        final ops = [
          _op('entry_state', 'E1', 'read', true, lamport: 7, device: 'A'),
          _op('entry_state', 'E1', 'starred', true, lamport: 7, device: 'A'),
        ];

        await sync.applyOps(invertido ? ops.reversed.toList() : ops);

        final estado = (await _estado(app, 'E1'))!;
        expect(estado['read'], 1, reason: 'invertido=$invertido');
        expect(estado['starred'], 1, reason: 'invertido=$invertido');
      }
    });

    test('ante el mismo conflicto los dos órdenes eligen lo mismo', () async {
      // Mismo campo, mismo reloj, dispositivos distintos: desempata el
      // device_id mayor, y eso no puede depender del orden de llegada.
      final resultados = <int>[];
      for (final invertido in [false, true]) {
        final (app, _, sync) = await _abrir();
        await _sembrar(app);
        final ops = [
          _op('entry_state', 'E1', 'read', true, lamport: 9, device: 'AAA'),
          _op('entry_state', 'E1', 'read', false, lamport: 9, device: 'ZZZ'),
        ];

        await sync.applyOps(invertido ? ops.reversed.toList() : ops);
        resultados.add((await _estado(app, 'E1'))!['read']! as int);
      }
      expect(resultados[0], resultados[1]);
      expect(resultados[0], 0, reason: 'gana ZZZ, que es el device_id mayor');
    });

    test('un reloj menor no revierte un cambio más reciente', () async {
      final (app, _, sync) = await _abrir();
      await _sembrar(app);

      await sync.applyOps([_op('entry_state', 'E1', 'read', true, lamport: 20, device: 'A')]);
      final tardia =
          await sync.applyOps([_op('entry_state', 'E1', 'read', false, lamport: 3, device: 'A')]);

      expect(tardia.ignored, 1);
      expect((await _estado(app, 'E1'))!['read'], 1);
    });
  });

  group('operaciones huérfanas', () {
    test('el estado de un artículo que aún no existe se aparca y se recupera',
        () async {
      final (app, _, sync) = await _abrir();
      // Sin sembrar: llega el estado antes que el artículo.
      final r = await sync.applyOps([
        _op('entry_state', 'E1', 'starred', true, lamport: 4, device: 'B'),
      ]);

      expect(r.pending, 1);
      expect(await _estado(app, 'E1'), isNull);
      expect(
        (await app.db.query('sync_pending')).length,
        1,
        reason: 'la operación no se pierde, queda aparcada',
      );

      await _sembrar(app);
      final recuperadas = await sync.replayPending();

      expect(recuperadas, 1);
      expect((await _estado(app, 'E1'))!['starred'], 1);
      expect((await app.db.query('sync_pending')), isEmpty);
    });
  });

  group('altas y bajas de artículos', () {
    test('una entrada nueva llega por el diario después del snapshot', () async {
      final (app, _, sync) = await _abrir();
      await app.db.insert('feeds', {
        'id': 'F1',
        'url': 'https://ejemplo/feed',
        'title': 'Feed',
      });

      final r = await sync.applyOps([
        _op('entry', 'NUEVA', 'data', {
          'id': 'NUEVA',
          'feed_id': 'F1',
          'url': 'https://ejemplo/nueva',
          'title': 'Recién publicada',
          'published_at': 2000,
        }, lamport: 5, device: 'HUB'),
      ]);

      expect(r.applied, 1);
      expect(await _estado(app, 'NUEVA'), isNotNull);
      expect((await app.db.query('entries', where: 'id = ?', whereArgs: ['NUEVA'])).length, 1);
    });

    test('el tombstone elimina la copia local y su estado', () async {
      final (app, _, sync) = await _abrir();
      await _sembrar(app);

      final r = await sync.applyOps([
        _op('entry', 'E1', 'deleted', true, lamport: 8, device: 'HUB'),
      ]);

      expect(r.applied, 1);
      expect(await _estado(app, 'E1'), isNull);
      expect(await app.db.query('entries', where: 'id = ?', whereArgs: ['E1']), isEmpty);
    });
  });

  group('lista blanca de campos', () {
    test('un campo desconocido se descarta en vez de llegar al SQL', () async {
      final (app, _, sync) = await _abrir();
      await _sembrar(app);

      // El nombre del campo viene de la red y acaba interpolado en el SQL: si
      // la lista blanca fallara, esto sería una inyección.
      final r = await sync.applyOps([
        _op('entry_state', 'E1', 'read = 1, starred', true, lamport: 2, device: 'B'),
        _op('inventada', 'X', 'read', true, lamport: 2, device: 'B'),
      ]);

      expect(r.applied, 0);
      expect(r.ignored, 2);
      expect(await _estado(app, 'E1'), isNull);
    });
  });

  group('escrituras locales', () {
    test('marcar leído deja el cambio en la cola de subida y sella el reloj',
        () async {
      final (app, repo, _) = await _abrir();
      await _sembrar(app);

      await repo.marcarLeido(['E1'], true);

      expect((await _estado(app, 'E1'))!['read'], 1);

      final cola = await repo.pendientesDeSubir();
      expect(cola.length, 1);
      expect(cola.first.$2.entity, 'entry_state');
      expect(cola.first.$2.field, 'read');
      expect(cola.first.$2.deviceId, await app.deviceId());

      final reloj = await app.db.query('field_clock',
          where: 'entity = ? AND entity_id = ? AND field = ?',
          whereArgs: ['entry_state', 'E1', 'read']);
      expect(reloj.length, 1);
      expect(reloj.first['lamport'], cola.first.$2.lamport);
    });

    test('la cola solo se vacía con los identificadores confirmados', () async {
      final (app, repo, _) = await _abrir();
      await _sembrar(app);
      await repo.marcarLeido(['E1'], true);
      await repo.marcarGuardado(['E1'], true);

      final cola = await repo.pendientesDeSubir();
      expect(cola.length, 2);

      await repo.limpiarOutbox([cola.first.$1]);
      expect((await repo.pendientesDeSubir()).length, 1);
    });

    test('una escritura local gana a una remota anterior', () async {
      final (app, repo, sync) = await _abrir();
      await _sembrar(app);

      await sync.applyOps([_op('entry_state', 'E1', 'read', true, lamport: 1, device: 'A')]);
      await repo.marcarLeido(['E1'], false);

      expect((await _estado(app, 'E1'))!['read'], 0);
    });
  });

  group('identificadores', () {
    test('el ULID tiene 26 caracteres de base32 de Crockford', () {
      final id = nuevoUlid();
      expect(id.length, 26);
      expect(RegExp(r'^[0-9A-HJKMNP-TV-Z]{26}$').hasMatch(id), isTrue);
    });

    test('los generados después ordenan después', () async {
      final primero = nuevoUlid();
      await Future<void>.delayed(const Duration(milliseconds: 3));
      expect(nuevoUlid().compareTo(primero), greaterThan(0));
    });
  });
}
