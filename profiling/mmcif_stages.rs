use datafusion_bio_format_structure::{batch_builder, mmcif, schema, StructureOptions};
use std::{hint::black_box, sync::Arc, time::Instant};
#[global_allocator]
static ALLOC: mimalloc::MiMalloc = mimalloc::MiMalloc;
fn main() {
    let args: Vec<_> = std::env::args().collect();
    let data = std::fs::read(&args[1]).unwrap();
    let repeats = args.get(2).map_or(3, |s| s.parse().unwrap());
    let options = StructureOptions::default();
    let all = schema::schema(&options);
    let names = [
        "source_index",
        "entry_index",
        "atom_index",
        "model_id",
        "chain_id",
        "auth_seq_id",
        "insertion_code",
        "atom_name",
        "residue_name",
        "alt_id",
        "x",
        "y",
        "z",
        "occupancy",
        "b_factor",
        "atom_id",
        "record_type",
        "auth_asym_id",
        "label_asym_id",
        "label_entity_id",
        "label_seq_id",
        "auth_atom_id",
        "label_atom_id",
        "auth_comp_id",
        "label_comp_id",
        "element",
        "formal_charge",
    ];
    let projection: Vec<_> = names.iter().map(|s| all.index_of(s).unwrap()).collect();
    let projected = Arc::new(all.project(&projection).unwrap());
    for repeat in 0..repeats {
        let start = Instant::now();
        let blocks = mmcif::Blocks::parse(black_box(&data)).unwrap();
        let parsed = Instant::now();
        let entry = blocks.entry(0, &options).unwrap().unwrap();
        let decoded = Instant::now();
        drop(blocks);
        let document_dropped = Instant::now();
        let batch = batch_builder::build(&entry, &options, projected.clone(), &projection).unwrap();
        let arrow = Instant::now();
        let rows = batch.num_rows();
        let atom_bytes = entry.atoms.capacity() * std::mem::size_of_val(&entry.atoms[0]);
        let atom_size = std::mem::size_of_val(&entry.atoms[0]);
        drop(entry);
        let entry_dropped = Instant::now();
        black_box(&batch);
        println!(
            "{}",
            serde_json::json!({"iteration":repeat,"rows":rows,"raw_document_seconds":(parsed-start).as_secs_f64(),"category_and_decode_seconds":(decoded-parsed).as_secs_f64(),"document_drop_seconds":(document_dropped-decoded).as_secs_f64(),"arrow_seconds":(arrow-document_dropped).as_secs_f64(),"entry_drop_seconds":(entry_dropped-arrow).as_secs_f64(),"total_seconds":(entry_dropped-start).as_secs_f64(),"atom_struct_size":atom_size,"atom_vec_capacity_bytes":atom_bytes})
        );
    }
}
