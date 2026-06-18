import asyncio
import aiohttp
import csv

# --- Pencil Sector Survey Systems ---

elws = [
    'Pencil Sector AA-Z c0','Pencil Sector AQ-Y c21','Pencil Sector BQ-Y c13',
    'Pencil Sector CQ-Y c45','Pencil Sector CQ-Y d44','Pencil Sector CQ-Y d86',
    'Pencil Sector DL-Y c8','Pencil Sector DQ-Y c15','Pencil Sector EL-Y d107',
    'Pencil Sector EL-Y d119','Pencil Sector EL-Y d121','Pencil Sector EL-Y d44',
    'Pencil Sector FB-X c1-28','Pencil Sector FW-W c1-39','Pencil Sector FW-W c1-7',
    'Pencil Sector GG-Y d68','Pencil Sector HH-V c2-25','Pencil Sector HR-V b2-9',
    'Pencil Sector HR-W d1-157','Pencil Sector HW-W c1-10','Pencil Sector IR-W d1-38',
    'Pencil Sector IR-W d1-87','Pencil Sector JC-U b3-6','Pencil Sector JC-V c2-45',
    'Pencil Sector KC-V c2-34','Pencil Sector LC-V c2-41','Pencil Sector QI-T c3-1',
    'Pencil Sector VT-A c23',
]

water_worlds = [
    'Pencil Sector FB-X c1-4','Pencil Sector HG-Y c24','Pencil Sector CQ-O b6-3',
    'Pencil Sector FL-Y d111','Pencil Sector FL-Y d126','Pencil Sector GL-Y c8',
    'Pencil Sector GW-W c1-47','Pencil Sector KC-V c2-28','Pencil Sector MM-W c1-34',
    'Pencil Sector YP-X b1-4',
]

bio_sigs = [
    'Pencil Sector NX-U c2-4','Pencil Sector YJ-A c12','Pencil Sector ND-S b4-5',
    'Pencil Sector WY-S b3-2','Pencil Sector NN-T c3-10','Pencil Sector CL-Y d73',
    'Pencil Sector AV-Y c35','Pencil Sector AF-A d6','Pencil Sector KM-W d1-81',
    'Pencil Sector UD-T b3-7',
]

icy_rings = [
    'Pencil Sector YZ-Y c5','Pencil Sector ST-Q b5-2','Pencil Sector OC-U a3-4',
    'Pencil Sector OM-V b2-1','Pencil Sector RI-S b4-7',
]

metallic_rings = [
    'Pencil Sector LR-V b2-2','Pencil Sector GW-W d1-18','Pencil Sector LS-T c3-11',
    'Pencil Sector TE-Q b5-9','Pencil Sector MR-W b1-5',
]

# Tag each system with its list membership
tags = {}
for s in elws:           tags.setdefault(s, []).append('elw')
for s in water_worlds:   tags.setdefault(s, []).append('water_world')
for s in bio_sigs:       tags.setdefault(s, []).append('bio_sig')
for s in icy_rings:      tags.setdefault(s, []).append('icy_ring')
for s in metallic_rings: tags.setdefault(s, []).append('metallic_ring')

all_systems = list(tags.keys())
print(f'Total unique systems: {len(all_systems)}')


async def query_batch(session, names):
    url = 'https://spansh.co.uk/api/systems/search'
    payload = {
        'filters': {'name': {'value': names, 'comparison': 'in'}},
        'size': 100,
        'page': 0,
    }
    async with session.post(url, json=payload,
                            timeout=aiohttp.ClientTimeout(total=30)) as r:
        data = await r.json()
        return data.get('results', [])


async def main():
    batches = [all_systems[i:i+50] for i in range(0, len(all_systems), 50)]
    all_results = []

    async with aiohttp.ClientSession() as session:
        for i, batch in enumerate(batches):
            results = await query_batch(session, batch)
            all_results.extend(results)
            print(f'Batch {i+1}/{len(batches)}: {len(results)} returned')
            await asyncio.sleep(1)

    found_names = {r['name'] for r in all_results}
    missing = [s for s in all_systems if s not in found_names]
    if missing:
        print(f'\nNot found in Spansh ({len(missing)}):')
        for m in missing:
            print(f'  {m}')

    output_file = 'pencil_sector_survey.csv'
    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'name', 'x', 'y', 'z', 'lists', 'population',
            'controlling_faction', 'government', 'security',
            'primary_economy', 'updated_at'
        ])
        for r in all_results:
            writer.writerow([
                r.get('name', ''),
                r.get('x', ''), r.get('y', ''), r.get('z', ''),
                '|'.join(tags.get(r.get('name', ''), [])),
                r.get('population', ''),
                r.get('controlling_minor_faction', ''),
                r.get('government', ''),
                r.get('security', ''),
                r.get('primary_economy', ''),
                r.get('updated_at', ''),
            ])

    print(f'\nDone — {len(all_results)} systems written to {output_file}')


if __name__ == '__main__':
    asyncio.run(main())
