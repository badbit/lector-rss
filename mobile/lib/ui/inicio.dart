/// Pantalla principal: vistas, carpetas y feeds con sus contadores.
library;

import 'package:flutter/material.dart';

import '../app_state.dart';
import '../models.dart';
import 'ajustes.dart';
import 'articulos.dart';

class InicioPage extends StatefulWidget {
  const InicioPage({super.key, required this.estado});

  final AppState estado;

  @override
  State<InicioPage> createState() => _InicioPageState();
}

class _InicioPageState extends State<InicioPage> {
  List<Folder> _carpetas = [];
  List<Feed> _feeds = [];
  int _pendientes = 0;
  bool _cargando = true;

  @override
  void initState() {
    super.initState();
    widget.estado.addListener(_alCambiarEstado);
    _cargar();
    if (widget.estado.configurado) {
      // Al abrir la app se sincroniza sola: es lo que uno espera al volver del
      // escritorio con cosas ya leídas.
      widget.estado.sincronizar();
    }
  }

  @override
  void dispose() {
    widget.estado.removeListener(_alCambiarEstado);
    super.dispose();
  }

  void _alCambiarEstado() => _cargar();

  Future<void> _cargar() async {
    final carpetas = await widget.estado.repo.carpetas();
    final feeds = await widget.estado.repo.feeds();
    final pendientes = await widget.estado.pendientesDeSubir();
    await widget.estado.refrescarContadores();
    if (!mounted) return;
    setState(() {
      _carpetas = carpetas;
      _feeds = feeds;
      _pendientes = pendientes;
      _cargando = false;
    });
  }

  void _abrir({
    required String titulo,
    String? feedId,
    String? folderId,
    bool sinLeer = false,
    bool guardados = false,
  }) {
    Navigator.of(context).push(MaterialPageRoute(
      builder: (_) => ArticulosPage(
        estado: widget.estado,
        titulo: titulo,
        feedId: feedId,
        folderId: folderId,
        soloSinLeer: sinLeer,
        soloGuardados: guardados,
      ),
    )).then((_) => _cargar());
  }

  @override
  Widget build(BuildContext context) {
    final estado = widget.estado;

    return Scaffold(
      appBar: AppBar(
        title: const Text('Lector RSS'),
        actions: [
          if (estado.sincronizando)
            const Padding(
              padding: EdgeInsets.symmetric(horizontal: 16),
              child: Center(
                child: SizedBox(
                  width: 18, height: 18,
                  child: CircularProgressIndicator(strokeWidth: 2),
                ),
              ),
            )
          else
            IconButton(
              tooltip: 'Sincronizar',
              icon: const Icon(Icons.sync),
              onPressed: () => estado.sincronizar(),
            ),
          IconButton(
            icon: const Icon(Icons.settings),
            onPressed: () => Navigator.of(context)
                .push(MaterialPageRoute(builder: (_) => AjustesPage(estado: estado)))
                .then((_) => _cargar()),
          ),
        ],
      ),
      body: _cargando
          ? const Center(child: CircularProgressIndicator())
          : RefreshIndicator(
              onRefresh: () async {
                await estado.sincronizar();
                await _cargar();
              },
              child: ListView(children: _contenido()),
            ),
    );
  }

  List<Widget> _contenido() {
    final estado = widget.estado;

    if (!estado.configurado) {
      return [
        const SizedBox(height: 80),
        const Icon(Icons.cloud_off, size: 56, color: Colors.grey),
        const SizedBox(height: 16),
        const Center(child: Text('Todavía no hay ningún hub configurado.')),
        const SizedBox(height: 8),
        Center(
          child: FilledButton(
            onPressed: () => Navigator.of(context)
                .push(MaterialPageRoute(builder: (_) => AjustesPage(estado: estado)))
                .then((_) => _cargar()),
            child: const Text('Configurar la conexión'),
          ),
        ),
      ];
    }

    final porCarpeta = <String?, List<Feed>>{};
    for (final feed in _feeds) {
      porCarpeta.putIfAbsent(feed.folderId, () => []).add(feed);
    }

    return [
      if (estado.ultimoError != null)
        Container(
          color: Colors.orange.shade100,
          padding: const EdgeInsets.all(12),
          child: Row(
            children: [
              const Icon(Icons.warning_amber, size: 18),
              const SizedBox(width: 8),
              Expanded(child: Text(estado.ultimoError!)),
            ],
          ),
        ),
      if (_pendientes > 0)
        ListTile(
          dense: true,
          leading: const Icon(Icons.cloud_upload_outlined, size: 20),
          title: Text('$_pendientes cambios sin subir'),
          subtitle: const Text('Se subirán en la próxima sincronización'),
        ),
      _vista(Icons.circle_notifications, 'Sin leer', estado.sinLeer,
          () => _abrir(titulo: 'Sin leer', sinLeer: true)),
      _vista(Icons.star, 'Guardados', null,
          () => _abrir(titulo: 'Guardados', guardados: true)),
      _vista(Icons.all_inbox, 'Todos los artículos', null,
          () => _abrir(titulo: 'Todos los artículos')),
      const Divider(),
      for (final carpeta in _carpetas)
        if ((porCarpeta[carpeta.id] ?? []).isNotEmpty)
          _carpeta(carpeta, porCarpeta[carpeta.id]!),
      for (final feed in porCarpeta[null] ?? <Feed>[]) _feed(feed),
      if (_feeds.isEmpty)
        const Padding(
          padding: EdgeInsets.all(32),
          child: Center(
            child: Text('Sin suscripciones todavía.\nSincroniza para traértelas del hub.',
                textAlign: TextAlign.center),
          ),
        ),
    ];
  }

  Widget _vista(IconData icono, String titulo, int? contador, VoidCallback alPulsar) =>
      ListTile(
        leading: Icon(icono),
        title: Text(titulo),
        trailing: (contador != null && contador > 0)
            ? Chip(label: Text('$contador'), visualDensity: VisualDensity.compact)
            : null,
        onTap: alPulsar,
      );

  Widget _carpeta(Folder carpeta, List<Feed> feeds) {
    final sinLeer = feeds.fold<int>(0, (suma, f) => suma + f.unread);
    return ExpansionTile(
      leading: const Icon(Icons.folder_outlined),
      title: Text(carpeta.name),
      trailing: sinLeer > 0
          ? Chip(label: Text('$sinLeer'), visualDensity: VisualDensity.compact)
          : null,
      childrenPadding: const EdgeInsets.only(left: 16),
      children: [
        ListTile(
          dense: true,
          leading: const Icon(Icons.list, size: 20),
          title: const Text('Toda la carpeta'),
          onTap: () => _abrir(titulo: carpeta.name, folderId: carpeta.id),
        ),
        for (final feed in feeds) _feed(feed),
      ],
    );
  }

  Widget _feed(Feed feed) => ListTile(
        dense: true,
        // Un icono distinto para lo que viene de raspar una web sin RSS.
        leading: Icon(feed.isScraped ? Icons.travel_explore : Icons.rss_feed, size: 20),
        title: Text(feed.displayTitle, overflow: TextOverflow.ellipsis),
        trailing: feed.unread > 0
            ? Text('${feed.unread}', style: const TextStyle(fontWeight: FontWeight.bold))
            : null,
        onTap: () => _abrir(titulo: feed.displayTitle, feedId: feed.id),
      );
}
